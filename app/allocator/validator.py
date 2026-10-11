"""Independent business-rule verification: do not trust the solver's own model."""

from collections import defaultdict
from datetime import date, timedelta
import math

from .candidates import weekly_capacities
from .models import AllocationConfig, Dataset


def validate_allocation(data: Dataset, config: AllocationConfig, allocations, unallocated=(), *, strict=True):
    errors = []
    used = defaultdict(int)
    seen = set()
    total_cost = total_carbon = 0.0
    late_orders = 0
    total_late_days = 0
    caps = weekly_capacities(
        data, use_events=config.use_capacity_events,
        additional_events=config.additional_capacity_events,
    )
    for row in allocations:
        order_id, plant, week = row["order_id"], row["plant"], row["week"]
        if order_id in seen or order_id not in data.orders:
            errors.append(f"Unknown or duplicate allocated order {order_id}")
            continue
        seen.add(order_id)
        order = data.orders[order_id]
        pp = data.products.get((plant, order.product))
        route = data.transport.get((plant, order.destination))
        if pp is None or route is None or (plant, week) not in caps:
            errors.append(f"Invalid plant/product/route/week for {order_id}")
            continue
        if plant in config.excluded_plants or (plant, order.product) in config.excluded_plant_products:
            errors.append(f"Excluded plant chosen for {order_id}")
        if order_id in config.forced_assignments and config.forced_assignments[order_id] != plant:
            errors.append(f"Forced plant not honored for {order_id}")

        used[plant, week] += order.qty
        arrival = data.start + timedelta(days=(week - 1) * 7 + 6 + route.days)
        late = max(0, (arrival - order.due).days)
        unit_subtotal = pp.unit_cost + pp.energy_kwh * pp.energy_price + route.cost
        cost = order.qty * unit_subtotal * (1 + route.duty)
        carbon = order.qty * (pp.energy_kwh * pp.weekly_carbon.get(week, pp.carbon_factor) + route.carbon)
        if row["arrival"] != arrival.isoformat() or row["lateness_days"] != late or row["on_time"] != (late == 0):
            errors.append(f"Arrival/lateness mismatch for {order_id}")
        if row["qty"] != order.qty or not math.isclose(row["cost"], cost, abs_tol=0.02) or not math.isclose(row["carbon_kg"], carbon, abs_tol=0.02):
            errors.append(f"Cost/carbon/quantity mismatch for {order_id}")
        if strict and late:
            errors.append(f"Order {order_id} delivered late in strict mode")
        late_orders += int(late > 0)
        total_late_days += late
        total_cost += cost
        total_carbon += carbon

    missing = list(unallocated)
    if len(set(missing)) != len(missing):
        errors.append("Duplicate unallocated IDs")
    if any(k in seen or k not in data.orders for k in missing):
        errors.append("Unallocated list contains invalid or allocated order")
    if set(data.orders) != (seen | set(missing)):
        errors.append("Orders missing from both allocated and unallocated lists")
    if strict and missing:
        errors.append("Strict allocation cannot leave orders unallocated")
    if any(k in config.forced_assignments for k in missing):
        errors.append("A forced-assignment order was left unallocated")
    for key, consumed in used.items():
        if consumed > caps[key]:
            errors.append(f"Plant {key[0]} week {key[1]} exceeded capacity: {consumed} > {caps[key]}")
    if config.max_cost is not None and total_cost > config.max_cost + 0.02:
        errors.append("Total cost exceeds specified budget")
    if config.max_carbon_kg is not None and total_carbon > config.max_carbon_kg + 0.02:
        errors.append("Total carbon exceeds specified budget")
    return {
        "valid": not errors,
        "capacity_ok": not any("exceeded capacity" in x for x in errors),
        "eligibility_ok": not any("Invalid plant/product" in x for x in errors),
        "delivery_ok": late_orders == 0,
        "errors": errors,
        "total_cost": round(total_cost, 2),
        "total_carbon_kg": round(total_carbon, 3),
        "late_orders": late_orders,
        "total_late_days": total_late_days,
        "unallocated_orders": len(missing),
        "plant_utilisation_by_week": {
            f"{plant}:W{week}": round(used[plant, week] / caps[plant, week], 4) if caps[plant, week] else 0.0
            for plant, week in sorted(caps)
        },
        "remaining_capacity_by_week": {
            f"{plant}:W{week}": caps[plant, week] - used[plant, week]
            for plant, week in sorted(caps)
        },
    }

