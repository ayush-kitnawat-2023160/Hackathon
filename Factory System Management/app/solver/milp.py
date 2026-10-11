"""solve_allocation: build the model and solve it with OR-Tools CP-SAT (or PuLP + CBC for comparison)."""
import time

import pandas as pd
from ortools.sat.python import cp_model

from app.config import cfg
from app.solver.model import UNMET_PENALTY, Model, build_model, closest_option


def _solve_cpsat(m: Model, time_limit: float, workers: int) -> tuple[str, list[int], set[str]]:
    s = cp_model.CpModel()
    x = [s.NewBoolVar(f"x{i}") for i in range(len(m.options))]
    by_order: dict[str, list[int]] = {}
    for i, oid in enumerate(m.options["order_id"]):
        by_order.setdefault(oid, []).append(i)
    u = {}
    for oid in m.orders:
        idx = by_order.get(oid, [])
        if oid in m.force_orders:
            s.Add(sum(x[i] for i in idx) == 1)
        else:
            u[oid] = s.NewBoolVar(f"u_{oid}")
            s.Add(sum(x[i] for i in idx) + u[oid] == 1)
    qty = m.options["qty"].astype(int).tolist()
    by_cap: dict[tuple, list[int]] = {}
    for i, k in enumerate(m.options["cap_key"]):
        by_cap.setdefault(k, []).append(i)
    for k, idx in by_cap.items():
        s.Add(sum(qty[i] * x[i] for i in idx) <= int(m.caps[k]))
    if m.carbon_cap_kg is not None:
        g = (m.options["carbon_kg"] * 1000).round().astype(int).tolist()
        s.Add(sum(g[i] * x[i] for i in range(len(x))) <= int(m.carbon_cap_kg * 1000))
    coef = m.options["coef"].tolist()
    s.Minimize(sum(coef[i] * x[i] for i in range(len(x))) + sum(UNMET_PENALTY * v for v in u.values()))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = workers
    solver.parameters.relative_gap_limit = cfg()["solver"]["gap"]
    st = solver.Solve(s)
    status = solver.StatusName(st)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return status, [], set()
    chosen = [i for i in range(len(x)) if solver.Value(x[i])]
    unmet = {oid for oid, v in u.items() if solver.Value(v)}
    return status, chosen, unmet


def _solve_pulp(m: Model, time_limit: float, workers: int) -> tuple[str, list[int], set[str]]:
    import pulp

    p = pulp.LpProblem("allocation", pulp.LpMinimize)
    x = [pulp.LpVariable(f"x{i}", cat="Binary") for i in range(len(m.options))]
    by_order: dict[str, list[int]] = {}
    for i, oid in enumerate(m.options["order_id"]):
        by_order.setdefault(oid, []).append(i)
    u = {}
    for n, oid in enumerate(m.orders):
        idx = by_order.get(oid, [])
        if oid in m.force_orders:
            p += pulp.lpSum(x[i] for i in idx) == 1
        else:
            u[oid] = pulp.LpVariable(f"u{n}", cat="Binary")
            p += pulp.lpSum(x[i] for i in idx) + u[oid] == 1
    qty = m.options["qty"].astype(int).tolist()
    by_cap: dict[tuple, list[int]] = {}
    for i, k in enumerate(m.options["cap_key"]):
        by_cap.setdefault(k, []).append(i)
    for k, idx in by_cap.items():
        p += pulp.lpSum(qty[i] * x[i] for i in idx) <= m.caps[k]
    if m.carbon_cap_kg is not None:
        kg = m.options["carbon_kg"].tolist()
        p += pulp.lpSum(kg[i] * x[i] for i in range(len(x))) <= m.carbon_cap_kg
    coef = m.options["coef"].tolist()
    p += pulp.lpSum(coef[i] * x[i] for i in range(len(x))) + pulp.lpSum(UNMET_PENALTY * v for v in u.values())
    p.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit, gapRel=cfg()["solver"]["gap"]))
    status = {1: "OPTIMAL", 0: "NOT_SOLVED", -1: "INFEASIBLE", -2: "UNBOUNDED"}.get(p.status, "UNKNOWN")
    if status != "OPTIMAL":
        return status, [], set()
    chosen = [i for i in range(len(x)) if (x[i].value() or 0) > 0.5]
    unmet = {oid for oid, v in u.items() if (v.value() or 0) > 0.5}
    return status, chosen, unmet


def solve_allocation(options: pd.DataFrame, orders: list[str], caps: dict, cap_mode: str, weights: dict,
                     policy: str = "never_late", baseline: dict[str, str] | None = None,
                     carbon_cap_kg: float | None = None, force_orders: set[str] | None = None,
                     backend: str = "cpsat", time_limit: float | None = None, workers: int | None = None) -> dict:
    """Return {status, allocation, unmet, flagged_late, refs, solve_ms}. Never invents a plan."""
    t0 = time.perf_counter()
    m = build_model(options, orders, caps, cap_mode, weights, policy, baseline, carbon_cap_kg, force_orders)
    limit = time_limit or cfg()["solver"]["time_limit_s"]
    status, chosen, unmet = (_solve_pulp if backend == "pulp" else _solve_cpsat)(
        m, limit, workers or cfg()["solver"]["workers"])
    unmet |= set(m.no_option)
    rows = m.options.iloc[chosen].drop(columns=["coef", "cap_key"]) if chosen else m.options.iloc[0:0]
    allocation = [{
        "order_id": r.order_id, "plant": r.plant, "week": int(r.week), "product": r.product, "qty": int(r.qty),
        "destination": r.destination, "due": r.due.isoformat(), "cost": round(float(r.landed_cost), 2),
        "carbon_kg": round(float(r.carbon_kg), 2), "arrival": r.arrival.isoformat(),
        "days_late": int(r.days_late), "on_time": bool(r.on_time),
        "delivery": "On time" if r.on_time else (
            "Late · no on-time option" if r.order_id in m.flagged else f"{int(r.days_late)} days late"),
    } for r in rows.sort_values("order_id").itertuples()]
    unmet_rows = []
    for oid in sorted(unmet):
        reason = ("No plant can make this product or ship to this destination" if oid in m.no_option
                  else "No capacity left in any eligible plant-week that meets the delivery policy")
        unmet_rows.append({"order_id": oid, "reason": reason, "closest_option": closest_option(options, oid)})
    return {"status": status, "backend": backend, "allocation": allocation, "unmet": unmet_rows,
            "flagged_late": sorted(m.flagged & {a["order_id"] for a in allocation}), "refs": m.refs,
            "solve_ms": round((time.perf_counter() - t0) * 1000)}


def greedy_baseline(options: pd.DataFrame, orders_df: pd.DataFrame, caps: dict, cap_mode: str) -> dict:
    """Level-2 comparison plan: earliest due first, cheapest plant-week with capacity left, ignoring delivery."""
    left = dict(caps)
    alloc, unmet = [], []
    for o in orders_df.sort_values(["due", "order_id"]).itertuples():
        opts = options[options["order_id"] == o.order_id].sort_values(["landed_cost", "week"])
        for r in opts.itertuples():
            k = (r.plant, None if cap_mode == "plant" else r.product, r.week)
            if left.get(k, 0) >= r.qty:
                left[k] -= r.qty
                alloc.append({"order_id": r.order_id, "plant": r.plant, "week": int(r.week),
                              "product": r.product, "qty": int(r.qty), "destination": r.destination,
                              "due": r.due.isoformat(), "cost": float(r.landed_cost),
                              "carbon_kg": float(r.carbon_kg), "arrival": r.arrival.isoformat(),
                              "days_late": int(r.days_late), "on_time": bool(r.on_time)})
                break
        else:
            unmet.append({"order_id": o.order_id, "reason": "greedy found no capacity", "closest_option": None})
    return {"status": "GREEDY", "backend": "greedy", "allocation": alloc, "unmet": unmet,
            "flagged_late": [], "refs": {}, "solve_ms": 0}
