"""CSV columns, file helpers, and challenge-case checks."""

import csv
from datetime import date, timedelta

DESTINATIONS = (
    "Germany", "France", "Japan", "Brazil", "India", "USA",
    "Canada", "UK", "Italy", "Australia", "Mexico", "UAE",
)
ORDER_FIELDS = ("order_id", "product", "qty", "destination", "due")
PLANT_FIELDS = (
    "plant", "product", "weekly_capacity", "unit_cost", "energy_kwh_per_unit",
    "carbon_kg_per_kwh", "energy_price_per_kwh", "carbon_kg_per_kwh_week_2",
    "carbon_kg_per_kwh_week_3",
)
TRANSPORT_FIELDS = (
    "plant", "destination", "transport_cost_per_unit", "transit_days",
    "duty_pct", "carbon_kg_per_unit",
)
EVENT_FIELDS = ("event_id", "plant", "from_date", "to_date", "capacity_loss_pct")
SCHEMA = {
    "orders": ORDER_FIELDS,
    "plants": PLANT_FIELDS,
    "transport_duties": TRANSPORT_FIELDS,
    "capacity_events": EVENT_FIELDS,
}
REQUIRED_FIELDS = {
    "orders": ORDER_FIELDS,
    "plants": PLANT_FIELDS[:6],
    "transport_duties": TRANSPORT_FIELDS,
    "capacity_events": EVENT_FIELDS,
}


def iso(day):
    return day.isoformat()


def write_csv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader), set(reader.fieldnames or [])


def landed_cost_per_unit(plant, route, apply_duty=True):
    """Duty applies to production, energy, and shipping."""
    subtotal = (
        float(plant["unit_cost"])
        + float(plant["energy_kwh_per_unit"]) * float(plant["energy_price_per_kwh"])
        + float(route["transport_cost_per_unit"])
    )
    return subtotal * (1 + float(route["duty_pct"])) if apply_duty else subtotal


def edge_case_audit(orders, plants, transport, events, start, *, exclusive_plant="C", exclusive_product="P_ONLY_C", weeks=4):
    p_lookup = {(p["plant"], p["product"]): p for p in plants}
    t_lookup = {(t["plant"], t["destination"]): t for t in transport}
    plant_names = sorted({p["plant"] for p in plants})
    capacities = {p["plant"]: int(p["weekly_capacity"]) for p in plants}
    first_week_end = start + timedelta(days=6)
    late_cheapest = []
    for order in orders:
        candidates = []
        for plant in plant_names:
            if (plant, order["product"]) not in p_lookup:
                continue
            p = p_lookup[(plant, order["product"])]
            t = t_lookup[(plant, order["destination"])]
            arrival = first_week_end + timedelta(days=int(t["transit_days"]))
            candidates.append((landed_cost_per_unit(p, t), plant, arrival <= date.fromisoformat(order["due"])))
        if candidates:
            cheapest = min(candidates)
            if not cheapest[2] and any(c[2] for c in candidates):
                late_cheapest.append(order["order_id"])

    p_a = p_lookup[("A", "P1")]
    p_b = p_lookup[("B", "P1")]
    t_a = t_lookup[("A", "Germany")]
    t_b = t_lookup[("B", "Germany")]
    a_before = landed_cost_per_unit(p_a, t_a, False)
    b_before = landed_cost_per_unit(p_b, t_b, False)
    a_after = landed_cost_per_unit(p_a, t_a, True)
    b_after = landed_cost_per_unit(p_b, t_b, True)

    exclusive_qty = sum(int(o["qty"]) for o in orders if o["product"] == exclusive_product)
    base_c_capacity = capacities[exclusive_plant] * weeks
    event_c_capacity = 0
    for week in range(weeks):
        monday = start + timedelta(days=week * 7)
        sunday = monday + timedelta(days=6)
        week_losses = [float(e["capacity_loss_pct"]) for e in events
                       if e["plant"] == exclusive_plant
                       and date.fromisoformat(e["from_date"]) <= sunday
                       and date.fromisoformat(e["to_date"]) >= monday]
        event_c_capacity += int(capacities[exclusive_plant] * (1 - max(week_losses, default=0)))

    result = {
        "cheapest_is_late_but_alternative_is_on_time_count": len(late_cheapest),
        "example_cheapest_late_order_ids": late_cheapest[:12],
        "exclusive_product": exclusive_product,
        "exclusive_product_eligible_plants": [p for p in plant_names if (p, exclusive_product) in p_lookup],
        "exclusive_plant": exclusive_plant,
        "exclusive_product_required_units": exclusive_qty,
        "exclusive_product_baseline_capacity": base_c_capacity,
        "exclusive_product_disrupted_capacity": event_c_capacity,
        "exclusive_product_unallocatable_units_after_event_at_least": max(0, exclusive_qty - event_c_capacity),
        "duty_reverses_A_B_Germany_P1": bool(a_before < b_before and a_after > b_after),
        "duty_example_per_unit": {
            "A_before_duty": round(a_before, 2), "B_before_duty": round(b_before, 2),
            "A_after_duty": round(a_after, 2), "B_after_duty": round(b_after, 2),
        },
        "low_carbon_low_capacity_C": bool(
            capacities[exclusive_plant] < min(capacities[p] for p in plant_names if p != exclusive_plant)
            and float(p_lookup[(exclusive_plant, "P1")]["carbon_kg_per_kwh"])
            < min(float(p_lookup[(p, "P1")]["carbon_kg_per_kwh"]) for p in plant_names if p != exclusive_plant)
        ),
        "carbon_varies_by_week": any(
            p["carbon_kg_per_kwh"] != p["carbon_kg_per_kwh_week_2"]
            for p in plants
        ),
    }
    if weeks == 4:
        result["exclusive_product_4week_baseline_capacity"] = base_c_capacity
        result["exclusive_product_4week_disrupted_capacity"] = event_c_capacity
    return result

