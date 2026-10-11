"""compare_plans: baseline versus scenario. (apply_scenario lives with the capacity code in models/options.py.)"""


def compare_plans(base: dict, scen: dict, base_kpis: dict, scen_kpis: dict,
                  eff_caps: dict | None = None) -> dict:
    b = {a["order_id"]: a for a in base["allocation"]}
    s = {a["order_id"]: a for a in scen["allocation"]}
    moved = []
    for oid, a in b.items():
        n = s.get(oid)
        if n is None:
            moved.append({"order_id": oid, "from": a["plant"], "to": None, "extra_cost": None,
                          "extra_carbon_kg": None, "delivery": "Cannot be met",
                          "why": _why_unmet(oid, scen)})
        elif (n["plant"], n["week"]) != (a["plant"], a["week"]):
            moved.append({"order_id": oid, "from": a["plant"], "to": n["plant"],
                          "from_week": a["week"], "to_week": n["week"],
                          "extra_cost": round(n["cost"] - a["cost"]),
                          "extra_carbon_kg": round(n["carbon_kg"] - a["carbon_kg"]),
                          "delivery": "On time" if n["on_time"] else f"{n['days_late']} days late",
                          "why": _why_moved(a, n, eff_caps)})
    n_base = max(1, len(b))
    moved_plant = [m for m in moved if m["to"] != m["from"]]
    # like for like: totals over orders placed in both plans, so dropping an order never looks like a saving
    common = [o for o in b if o in s]
    dropped = [o for o in b if o not in s]
    return {
        "extra_cost": round(sum(s[o]["cost"] for o in common) - sum(b[o]["cost"] for o in common)),
        "extra_carbon_t": round((sum(s[o]["carbon_kg"] for o in common) - sum(b[o]["carbon_kg"] for o in common)) / 1000, 1),
        "orders_dropped": len(dropped),
        "dropped_baseline_cost": round(sum(b[o]["cost"] for o in dropped)),
        "late_orders_before": base_kpis["late_orders"], "late_orders_after": scen_kpis["late_orders"],
        "delta_late_orders": scen_kpis["late_orders"] - base_kpis["late_orders"],
        "infeasible_after": scen_kpis["infeasible_orders"],
        "orders_moved": len(moved_plant), "orders_rescheduled": len(moved) - len(moved_plant),
        "pct_unchanged": round(100 * (1 - len(moved) / n_base)),
        "cost_before": base_kpis["total_cost"], "cost_after": scen_kpis["total_cost"],
        "carbon_before_t": base_kpis["total_carbon_t"], "carbon_after_t": scen_kpis["total_carbon_t"],
        "moved": moved,
    }


def _why_moved(a: dict, n: dict, eff_caps: dict | None) -> str:
    if eff_caps:
        reduced = [k for k in eff_caps if k[0] == a["plant"] and k[2] == a["week"]]
        if reduced:
            return f"Plant {a['plant']} over its reduced capacity in week {a['week']}"
    if n["plant"] == a["plant"]:
        return f"Same plant, moved from week {a['week']} to week {n['week']} to free capacity"
    return f"Plant {n['plant']} is the next best valid plant once Plant {a['plant']} was full"


def _why_unmet(oid: str, scen: dict) -> str:
    for u in scen.get("unmet", []):
        if u["order_id"] == oid:
            return u["reason"]
    return "Not placed in the scenario plan"
