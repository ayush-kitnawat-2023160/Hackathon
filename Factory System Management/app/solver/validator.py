"""validate_plan: an independent re-check of any plan against Annex A.

Deliberately does not reuse the solver's option table or capacity code: it recomputes eligibility,
capacity use, arrival dates and totals straight from the raw tables, so a bug in the calculator or
solver shows up here instead of in front of the user.
"""
from datetime import date, timedelta

import pandas as pd

TOL = 0.01


def _effective_capacity(plants: pd.DataFrame, plant: str, product: str, week: int, start: date,
                        events: list[dict], per_plant: bool) -> float:
    rows = plants[(plants["plant"] == plant) & (per_plant | (plants["product"] == product))]
    if "week_start" in rows:
        rows = rows[rows["week_start"].isna()]
    if rows.empty:
        return 0.0
    cap = float(rows["weekly_capacity"].iloc[0])
    week_first = start + timedelta(days=7 * (week - 1))
    for e in events:
        if e["plant"] != plant:
            continue
        days = sum(1 for d in range(7) if e["from_date"] <= week_first + timedelta(days=d) <= e["to_date"])
        cap *= 1 - e["capacity_loss_pct"] / 100 * days / 7
    return cap


def validate_plan(plan: dict, t: dict[str, pd.DataFrame], start: date, policy: str = "never_late",
                  events: list[dict] | None = None) -> dict:
    events = events or []
    orders, plants, lanes = t["orders"], t["plants"], t["transport_duties"]
    per_plant = bool((plants.groupby("plant")["weekly_capacity"].nunique() <= 1).all())
    violations = []
    alloc = plan.get("allocation", [])
    placed = [a["order_id"] for a in alloc]
    unmet = {u["order_id"] for u in plan.get("unmet", [])}

    # every order exactly once, either placed or explicitly unmet
    dup = {o for o in placed if placed.count(o) > 1}
    missing = set(orders["order_id"]) - set(placed) - unmet
    for o in sorted(dup):
        violations.append({"rule": "assignment", "order_id": o, "detail": "order assigned more than once"})
    for o in sorted(missing):
        violations.append({"rule": "assignment", "order_id": o, "detail": "order neither placed nor reported unmet"})

    makes = set(zip(plants["plant"], plants["product"], strict=False))
    lane = {(r.plant, r.destination): r for r in lanes.itertuples()}
    od = orders.set_index("order_id")
    use: dict[tuple, float] = {}
    eligibility_ok = delivery_ok = True
    for a in alloc:
        o = od.loc[a["order_id"]]
        if (a["plant"], o["product"]) not in makes:
            eligibility_ok = False
            violations.append({"rule": "eligibility", "order_id": a["order_id"],
                               "detail": f"Plant {a['plant']} cannot make {o['product']}"})
            continue
        ln = lane.get((a["plant"], o["destination"]))
        if ln is None:
            eligibility_ok = False
            violations.append({"rule": "eligibility", "order_id": a["order_id"],
                               "detail": f"No transport lane {a['plant']} -> {o['destination']}"})
            continue
        key = (a["plant"], None if per_plant else o["product"], a["week"])
        use[key] = use.get(key, 0) + int(o["qty"])
        arrival = start + timedelta(days=7 * a["week"] - 1 + int(ln.transit_days))
        if arrival.isoformat() != a["arrival"] or (arrival <= o["due"]) != a["on_time"]:
            delivery_ok = False
            violations.append({"rule": "delivery", "order_id": a["order_id"],
                               "detail": f"reported arrival {a['arrival']} but recomputed {arrival}"})
        if arrival > o["due"] and policy == "never_late":
            # allowed only if no plant could ever deliver on time (week 1, fastest eligible lane)
            best = min((start + timedelta(days=6 + int(lane[(p, o["destination"])].transit_days))
                        for p, pr in makes if pr == o["product"] and (p, o["destination"]) in lane), default=None)
            if best is not None and best <= o["due"]:
                delivery_ok = False
                violations.append({"rule": "delivery", "order_id": a["order_id"],
                                   "detail": f"late ({arrival} > due {o['due']}) although an on-time plant exists"})

    capacity_ok = True
    for (plant, product, week), used in sorted(use.items(), key=str):
        cap = _effective_capacity(plants, plant, product, week, start, events, per_plant)
        if used > cap + TOL:
            capacity_ok = False
            violations.append({"rule": "capacity", "order_id": None,
                               "detail": f"Plant {plant} week {week}: {used} units > capacity {cap:.0f}"})
    return {"capacity_ok": capacity_ok, "eligibility_ok": eligibility_ok, "delivery_ok": delivery_ok,
            "all_orders_accounted": not dup and not missing, "violations": violations,
            "valid": capacity_ok and eligibility_ok and delivery_ok and not dup and not missing}
