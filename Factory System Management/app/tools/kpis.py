"""compute_kpis: plan totals and plant utilisation. get_kpis: KPIs of stored runs (for the brief)."""


def compute_kpis(plan: dict, caps: dict[tuple, float]) -> dict:
    alloc = plan["allocation"]
    plants = sorted({k[0] for k in caps})
    cap_by_plant = {p: sum(v for k, v in caps.items() if k[0] == p) for p in plants}
    used = {p: sum(a["qty"] for a in alloc if a["plant"] == p) for p in plants}
    n = len(alloc)
    return {
        "total_cost": round(sum(a["cost"] for a in alloc)),
        "total_carbon_t": round(sum(a["carbon_kg"] for a in alloc) / 1000, 1),
        "late_orders": sum(not a["on_time"] for a in alloc),
        "infeasible_orders": len(plan.get("unmet", [])),
        "orders_placed": n,
        "plant_utilisation": {p: round(used[p] / cap_by_plant[p], 2) if cap_by_plant[p] else 0.0
                              for p in plants},
        "plant_share": {p: round(sum(a["plant"] == p for a in alloc) / n, 2) if n else 0.0 for p in plants},
    }
