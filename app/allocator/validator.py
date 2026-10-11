"""Recalculate results without using solver variables or candidate coefficients."""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
import math

from .models import AllocationConfig, Dataset


def validate_allocation(data: Dataset, config: AllocationConfig, allocations, unallocated=(), *, strict=True):
    errors, seen = [], set()
    used = defaultdict(int)
    total_cost = total_carbon = 0.0
    late_orders = total_late_days = 0
    capacity_ok = eligibility_ok = delivery_ok = True
    excluded_pairs = {tuple(pair) for pair in config.excluded_plant_products}
    events = tuple(data.events if config.use_capacity_events else ()) + tuple(config.additional_capacity_events)
    caps = {}
    for plant, full in data.capacities.items():
        for week in range(1, data.weeks + 1):
            monday = data.start + timedelta(days=(week - 1) * 7)
            sunday = monday + timedelta(days=6)
            loss = max((event.loss for event in events
                        if event.plant == plant and event.start <= sunday and event.end >= monday), default=0)
            caps[plant, week] = int(Decimal(full) * (1 - Decimal(str(loss))))

    if not isinstance(allocations, (list, tuple)):
        errors.append("Allocation must be a list of rows")
        allocations = []
    required = {"order_id", "plant", "week", "qty", "cost", "carbon_kg", "arrival", "on_time", "lateness_days"}
    for row in allocations:
        if not isinstance(row, dict) or not required <= row.keys():
            errors.append("Allocation row is missing required fields")
            continue
        order_id, plant, week = row["order_id"], row["plant"], row["week"]
        if not isinstance(order_id, str) or not isinstance(plant, str) or type(week) is not int:
            errors.append("Order ID/plant must be strings and week must be an integer")
            eligibility_ok = False
            continue
        if order_id in seen or order_id not in data.orders:
            errors.append(f"Unknown or duplicate allocated order {order_id}")
            continue
        seen.add(order_id)
        order = data.orders[order_id]
        pp = data.products.get((plant, order.product))
        route = data.transport.get((plant, order.destination))
        if pp is None or route is None or (plant, week) not in caps:
            errors.append(f"Invalid plant/product/route/week for {order_id}")
            eligibility_ok = False
            continue
        if plant in config.excluded_plants or (plant, order.product) in excluded_pairs:
            errors.append(f"Excluded plant chosen for {order_id}")
            eligibility_ok = False
        if order_id in config.forced_assignments and config.forced_assignments[order_id] != plant:
            errors.append(f"Forced plant not honored for {order_id}")
            eligibility_ok = False
        if order_id in config.frozen_assignments and tuple(config.frozen_assignments[order_id]) != (plant, week):
            errors.append(f"Frozen assignment not honored for {order_id}")
            eligibility_ok = False

        used[plant, week] += order.qty
        arrival = data.start + timedelta(days=(week - 1) * 7 + 6 + route.days)
        late = max(0, (arrival - order.due).days)
        subtotal = pp.unit_cost + pp.energy_kwh * pp.energy_price + route.cost
        cost = order.qty * subtotal * (1 + route.duty)
        carbon = order.qty * (pp.energy_kwh * pp.weekly_carbon.get(week, pp.carbon_factor) + route.carbon)
        if (row["arrival"] != arrival.isoformat() or type(row["lateness_days"]) is not int
                or row["lateness_days"] != late or type(row["on_time"]) is not bool
                or row["on_time"] != (late == 0)):
            errors.append(f"Arrival/lateness mismatch for {order_id}")
            delivery_ok = False
        amounts_ok = all(type(row[key]) in (int, float) and math.isfinite(row[key]) for key in ("cost", "carbon_kg"))
        if (type(row["qty"]) is not int or row["qty"] != order.qty or not amounts_ok
                or not math.isclose(row["cost"], round(cost, 2), rel_tol=0, abs_tol=1e-8)
                or not math.isclose(row["carbon_kg"], round(carbon, 3), rel_tol=0, abs_tol=1e-8)):
            errors.append(f"Cost/carbon/quantity mismatch for {order_id}")
        if strict and late:
            errors.append(f"Order {order_id} delivered late in strict mode")
        late_orders += int(late > 0)
        total_late_days += late
        total_cost += cost
        total_carbon += carbon

    if not isinstance(unallocated, (list, tuple)) or any(not isinstance(oid, str) for oid in unallocated):
        errors.append("Unallocated orders must be a list of order IDs")
        unallocated = []
    missing = list(unallocated)
    if len(set(missing)) != len(missing):
        errors.append("Duplicate unallocated IDs")
    if any(oid in seen or oid not in data.orders for oid in missing):
        errors.append("Unallocated list contains invalid or allocated order")
    if set(data.orders) != seen | set(missing):
        errors.append("Orders missing from both allocated and unallocated lists")
    if strict and missing:
        errors.append("Strict allocation cannot leave orders unallocated")
    if any(oid in config.forced_assignments or oid in config.frozen_assignments for oid in missing):
        errors.append("A forced or frozen order was left unallocated")
    for key, consumed in used.items():
        if consumed > caps[key]:
            errors.append(f"Plant {key[0]} week {key[1]} exceeded capacity: {consumed} > {caps[key]}")
            capacity_ok = False
    if not math.isfinite(total_cost) or not math.isfinite(total_carbon):
        errors.append("Calculated totals are not finite")
    if config.max_cost is not None and total_cost > config.max_cost + 1e-6:
        errors.append("Total cost exceeds specified budget")
    if config.max_carbon_kg is not None and total_carbon > config.max_carbon_kg + 1e-6:
        errors.append("Total carbon exceeds specified budget")
    return {
        "valid": not errors,
        "capacity_ok": capacity_ok,
        "eligibility_ok": eligibility_ok,
        "delivery_ok": delivery_ok and late_orders == 0 and not missing and len(seen) == len(data.orders),
        "errors": errors,
        "total_cost": round(total_cost, 2),
        "total_carbon_kg": round(total_carbon, 3),
        "raw_total_cost": total_cost,
        "raw_total_carbon_kg": total_carbon,
        "late_orders": late_orders,
        "total_late_days": total_late_days,
        "unallocated_orders": len(missing),
        "plant_utilisation_by_week": {
            f"{plant}:W{week}": round(used[plant, week] / cap, 4) if cap else 0.0
            for (plant, week), cap in sorted(caps.items())
        },
        "remaining_capacity_by_week": {
            f"{plant}:W{week}": cap - used[plant, week]
            for (plant, week), cap in sorted(caps.items())
        },
    }
