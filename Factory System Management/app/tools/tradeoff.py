"""tradeoff_frontier: re-solve with carbon capped at 0-20% below the current plan. Every point is a solver run.

The cut points are independent, so they run in parallel, each with its own (shorter) time limit; a point
that hits the limit reports the best valid plan found (status FEASIBLE), never an estimate.
"""
from concurrent.futures import ThreadPoolExecutor

from app.config import cfg
from app.tools.planner import Ctx, run_plan


def tradeoff_frontier(ctx: Ctx, weights: dict, policy: str, base: dict, cuts: list[float] | None = None) -> list[dict]:
    cuts = cuts if cuts is not None else cfg()["tradeoff_cuts_pct"]
    base_carbon_kg = sum(a["carbon_kg"] for a in base["plan"]["allocation"])
    placed = {a["order_id"] for a in base["plan"]["allocation"]}
    sc = cfg()["solver"]
    todo = [c for c in cuts if c != 0]
    workers = max(1, sc["workers"] // max(1, len(todo)))

    def solve(cut):
        # orders placed in the current plan must stay placed, so carbon cannot be cut by dropping orders
        return run_plan(ctx, weights, policy, carbon_cap_kg=base_carbon_kg * (1 - cut / 100), force_orders=placed,
                        time_limit=sc["frontier_time_limit_s"], workers=workers)

    with ThreadPoolExecutor(max_workers=max(1, len(todo))) as ex:
        results = dict(zip(todo, ex.map(solve, todo), strict=False))
    points = []
    for cut in cuts:
        r = base if cut == 0 else results[cut]
        ok = r["kpis"] is not None and r["validator"] and r["validator"]["valid"]
        if not ok:
            points.append({"carbon_cut_pct": cut, "feasible": False, "cost": None, "carbon_t": None,
                           "late_orders": None, "extra_cost": None, "status": r["plan"]["status"]})
            continue
        k = r["kpis"]
        points.append({"carbon_cut_pct": cut, "feasible": True, "cost": k["total_cost"],
                       "carbon_t": k["total_carbon_t"], "late_orders": k["late_orders"],
                       "extra_cost": k["total_cost"] - base["kpis"]["total_cost"],
                       "status": r["plan"]["status"]})
    for p in points:
        cut_t = base["kpis"]["total_carbon_t"] - (p["carbon_t"] or 0)
        p["implied_price_per_t"] = round(p["extra_cost"] / cut_t) if p["feasible"] and cut_t > 0 else None
    return points
