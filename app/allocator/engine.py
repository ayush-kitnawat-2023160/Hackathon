"""Mixed-integer allocation: entire order -> eligible plant + production week.

Only PuLP chooses assignments. No LLM, LangGraph, API, or database dependency.
"""

from collections import defaultdict
from dataclasses import asdict
from datetime import date
from pathlib import Path
import tempfile
import uuid

from .candidates import create_candidates, weekly_capacities
from .loader import load_dataset
from .models import AllocationConfig, Dataset
from .validator import validate_allocation


def _pulp():
    try:
        import pulp
        return pulp
    except ImportError as e:
        raise RuntimeError(
            "PuLP is required. Install: python3 -m pip install --target .allocator-deps 'PuLP==3.3.0'; "
            "then run with PYTHONPATH=.allocator-deps python3 -m app.allocator.cli ..."
        ) from e


def _jsonable(value):
    """Convert typed scenario inputs to clean JSON-compatible trace fields."""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _build_model(data, config, candidates, *, best_effort=False):
    pulp = _pulp()
    caps = weekly_capacities(data, use_events=config.use_capacity_events,
                             additional_events=config.additional_capacity_events)
    allowed = [c for c in candidates
               if (best_effort or c.late_days == 0)
               and c.plant not in config.excluded_plants
               and (c.plant, data.orders[c.order_id].product) not in config.excluded_plant_products
               and (c.order_id not in config.forced_assignments or config.forced_assignments[c.order_id] == c.plant)
               and caps[c.plant, c.week] >= data.orders[c.order_id].qty]
    model = pulp.LpProblem("MultiPlantAllocation", pulp.LpMinimize)
    x = {c.key: pulp.LpVariable(f"choose_{i}", cat="Binary") for i, c in enumerate(allowed)}
    by_order = defaultdict(list)
    by_plant_week = defaultdict(list)
    for c in allowed:
        by_order[c.order_id].append(c)
        by_plant_week[c.plant, c.week].append(c)

    u = {}
    if best_effort:
        u = {order_id: pulp.LpVariable(f"unallocated_{i}", cat="Binary")
             for i, order_id in enumerate(data.orders)
             if order_id not in config.forced_assignments}

    for order_id in data.orders:
        model += (pulp.lpSum(x[c.key] for c in by_order[order_id])
                  + (u[order_id] if order_id in u else 0) == 1), f"one_assignment_{order_id}"
    for (plant, week), cap in caps.items():
        model += pulp.lpSum(data.orders[c.order_id].qty * x[c.key]
                            for c in by_plant_week[plant, week]) <= cap, f"capacity_{plant}_{week}"

    cost = pulp.lpSum(c.cost * x[c.key] for c in allowed)
    carbon = pulp.lpSum(c.carbon_kg * x[c.key] for c in allowed)
    if config.max_cost is not None:
        model += cost <= config.max_cost, "cost_budget"
    if config.max_carbon_kg is not None:
        model += carbon <= config.max_carbon_kg, "carbon_budget"

    # Stable normalization based on per-order cheapest (or lowest-carbon)
    # candidates. Same data and candidate set means identical scaling.
    cost_ref = max(1, sum(min((c.cost for c in by_order[oid]), default=0) for oid in data.orders))
    carbon_ref = max(1, sum(min((c.carbon_kg for c in by_order[oid]), default=0) for oid in data.orders))
    if config.objective == "cost":
        primary = cost / cost_ref
    elif config.objective == "carbon":
        primary = carbon / carbon_ref
    else:
        weight_sum = config.cost_weight + config.carbon_weight
        primary = (config.cost_weight / weight_sum) * cost / cost_ref + (config.carbon_weight / weight_sum) * carbon / carbon_ref

    if config.stability_weight and config.previous_plan:
        # Reassigning an existing order costs a small amount of objective weight.
        # Optional preference only; it NEVER weakens capacity/eligibility rules.
        changes = pulp.lpSum(x[c.key] for c in allowed
                             if c.order_id in config.previous_plan
                             and (c.plant, c.week) != tuple(config.previous_plan[c.order_id]))
        primary += config.stability_weight * changes / len(data.orders)
    return model, x, u, allowed, primary


def _execute(model, objective, *, seconds):
    pulp = _pulp()
    model.setObjective(objective)
    # PuLP/CBC sometimes maps a time-limited incumbent to LpStatus 'Optimal'.
    # Read the solver's proof status, not just its solution-file label.
    with tempfile.TemporaryDirectory(prefix="triplan-cbc-") as folder:
        log_path = Path(folder) / "cbc.log"
        model.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=seconds, logPath=str(log_path)))
        solver_log = log_path.read_text(errors="replace") if log_path.exists() else ""
    status = pulp.LpStatus[model.status]
    if status == "Optimal" and "Result - Optimal solution found" not in solver_log:
        return "Not Solved"  # Feasible incumbent can still be validated and labeled FEASIBLE.
    return status


def _extract(data, config, x, u, candidates, *, status, mode):
    pulp = _pulp()
    # CBC may supply an incumbent even after the time limit. Always validate
    # it independently, and never label it as proven OPTIMAL unless it is.
    chosen = [c for c in candidates if pulp.value(x[c.key]) is not None and pulp.value(x[c.key]) > 0.5]
    unallocated = sorted(oid for oid, value in u.items()
                         if pulp.value(value) is not None and pulp.value(value) > 0.5)
    rows = [
        {
            "order_id": c.order_id, "plant": c.plant, "week": c.week,
            "qty": data.orders[c.order_id].qty, "cost": round(c.cost, 2),
            "carbon_kg": round(c.carbon_kg, 3), "arrival": c.arrival.isoformat(),
            "on_time": c.late_days == 0, "lateness_days": c.late_days,
        }
        for c in sorted(chosen, key=lambda c: c.order_id)
    ]
    audit = validate_allocation(data, config, rows, unallocated, strict=(mode == "strict"))
    verified = audit["valid"]
    # 'Not Solved' can have feasible incumbents; never claim OPTIMAL for them.
    result_status = ("OPTIMAL" if status == "Optimal" else "FEASIBLE") if verified else "ERROR"
    if verified and mode == "best_effort":
        result_status = "BEST_EFFORT"
    unfilled_quantity = sum(data.orders[oid].qty for oid in unallocated if oid in data.orders)
    return {
        "run_id": f"al-{uuid.uuid4().hex[:10]}",
        "status": result_status,
        "solver_status": status,
        "mode": mode,
        "dataset_source": data.source,
        "allocation": rows if verified else [],
        "unallocated_order_ids": unallocated if verified else [],
        "kpis": {
            "total_cost": audit["total_cost"],
            "total_carbon_kg": audit["total_carbon_kg"],
            "total_carbon_t": round(audit["total_carbon_kg"] / 1000, 6),
            "late_orders": audit["late_orders"],
            "total_late_days": audit["total_late_days"],
            "unallocated_orders": audit["unallocated_orders"],
            "unallocated_quantity": unfilled_quantity,
            "all_orders_fulfilled": unfilled_quantity == 0,
            "plant_utilisation": audit["plant_utilisation_by_week"],
        },
        "validator": audit,
        "parameters_used": _jsonable({
            k: v for k, v in asdict(config).items() if k != "previous_plan"
        }),
        "assumptions": [
            "Whole order assigned to one plant and one production week.",
            "Weekly capacity shared across products for each plant.",
            "Production completes Sunday; arrival is Sunday + transit days.",
            "Synthetic landed-cost convention: duty on production + energy + transport.",
            "Overlapping capacity events use the maximum loss for each affected week.",
        ],
        "limitations": [
            "Underlying energy price and production timing conventions must be confirmed for official data.",
            "BEST_EFFORT results can contain late/unallocated orders and must not be labeled fully feasible.",
        ],
    }


def solve_allocation(dataset: Dataset | str | Path, config: AllocationConfig | dict | None = None, **loader_kwargs):
    """Callable by CLI now, or a teammate's LangGraph tool later.

    Args:
      dataset: validated Dataset object or path containing the 4 CSV files.
      config: typed AllocationConfig or JSON-like dict (not natural language).
      **loader_kwargs: start_date, weeks, energy_price_default when path supplied.
    """
    data = load_dataset(dataset, **loader_kwargs) if isinstance(dataset, (str, Path)) else dataset
    conf = config if isinstance(config, AllocationConfig) else AllocationConfig.from_dict(config)
    for plant in conf.excluded_plants:
        if plant not in data.capacities:
            raise ValueError(f"Unknown excluded plant: {plant}")
    for plant, product in conf.excluded_plant_products:
        if plant not in data.capacities:
            raise ValueError(f"Unknown excluded plant {plant}")
    for order_id, plant in conf.forced_assignments.items():
        if order_id not in data.orders or plant not in data.capacities:
            raise ValueError(f"Unknown forced assignment: {order_id} -> {plant}")
    for e in conf.additional_capacity_events:
        if e.plant not in data.capacities:
            raise ValueError(f"Scenario targets unknown plant {e.plant}")

    candidates = create_candidates(data)
    model, x, u, choices, obj = _build_model(data, conf, candidates, best_effort=False)
    solver_status = _execute(model, obj, seconds=conf.time_limit_seconds)
    if solver_status in ("Optimal", "Not Solved"):
        result = _extract(data, conf, x, u, choices, status=solver_status, mode="strict")
        if result["status"] != "ERROR":
            return result
    if not conf.allow_fallback:
        return {"status": "INFEASIBLE" if solver_status == "Infeasible" else solver_status.upper().replace(" ", "_"),
                "solver_status": solver_status, "mode": "strict", "allocation": [],
                "reason": "No fully on-time valid allocation confirmed for these constraints"}

    # Best effort: keep the same hard capacity/eligibility/budget constraints.
    # Optimization is lexicographic: (1) fewest unallocated orders,
    # (2) fewest unallocated units, (3) fewest late orders,
    # (4) fewest total late days, (5) chosen cost/carbon objective.
    model, x, u, choices, primary = _build_model(data, conf, candidates, best_effort=True)
    pulp = _pulp()
    steps = (
        ("unallocated_count", pulp.lpSum(u.values())),
        ("unallocated_units", pulp.lpSum(data.orders[oid].qty * variable for oid, variable in u.items())),
        ("late_orders", pulp.lpSum((c.late_days > 0) * x[c.key] for c in choices)),
        ("late_days", pulp.lpSum(c.late_days * x[c.key] for c in choices)),
        ("final_objective", primary),
    )
    last_status = "Not Solved"
    for idx, (name, expression) in enumerate(steps):
        last_status = _execute(model, expression, seconds=conf.time_limit_seconds)
        if last_status != "Optimal":
            # No safe claim of lexicographic priority if a stage is not proven optimal.
            return {"status": "INFEASIBLE" if last_status == "Infeasible" else "NOT_PROVEN",
                    "solver_status": last_status, "mode": "best_effort", "allocation": [],
                    "reason": f"Best-effort stage {name} did not reach proven optimality"}
        if idx < len(steps) - 1:
            # PuLP represents an identically-zero objective with value None.
            # Its actual optimum is zero (e.g. all orders necessarily unallocated).
            optimum = pulp.value(expression)
            if optimum is None:
                optimum = 0.0
            model += expression <= optimum + 1e-5, f"lock_{name}"
    return _extract(data, conf, x, u, choices, status=last_status, mode="best_effort")


def compare_plans(before, after):
    """Compare two VALID allocator results without an LLM."""
    if not before.get("validator", {}).get("valid") or not after.get("validator", {}).get("valid"):
        raise ValueError("Both plans must pass independent validation")
    a = {r["order_id"]: (r["plant"], r["week"]) for r in before["allocation"]}
    b = {r["order_id"]: (r["plant"], r["week"]) for r in after["allocation"]}
    changed = [oid for oid in sorted(set(a) | set(b)) if a.get(oid) != b.get(oid)]
    same_orders_served = set(a) == set(b)
    return {
        "before_run_id": before["run_id"], "after_run_id": after["run_id"],
        "orders_moved": len(changed), "moved_order_ids": changed,
        "cost_delta": round(after["kpis"]["total_cost"] - before["kpis"]["total_cost"], 2),
        "carbon_delta_kg": round(after["kpis"]["total_carbon_kg"] - before["kpis"]["total_carbon_kg"], 3),
        "late_orders_delta": after["kpis"]["late_orders"] - before["kpis"]["late_orders"],
        "unallocated_orders_delta": after["kpis"]["unallocated_orders"] - before["kpis"]["unallocated_orders"],
        "same_orders_served": same_orders_served,
        "comparison_warning": None if same_orders_served else (
            "Different orders were fulfilled. Lower cost/carbon can result from unallocated demand; "
            "do not describe this as an operational saving without considering unmet orders."
        ),
    }


def solve_tradeoff(dataset, carbon_cut_pct=10, **loader_kwargs):
    """Answer 'What does a 10% carbon reduction cost?' via TWO solver calls.

    Runs a strict cheapest on-time baseline, then cost-minimizes subject to a
    relative carbon cap. If baseline is not fully feasible, state that fact.
    """
    if not 0 <= carbon_cut_pct <= 100:
        raise ValueError("carbon_cut_pct must be between 0 and 100")
    baseline = solve_allocation(dataset, {"objective": "cost", "allow_fallback": False}, **loader_kwargs)
    if not baseline.get("validator", {}).get("valid"):
        return {"status": "BASELINE_INFEASIBLE", "baseline": baseline}
    target = baseline["kpis"]["total_carbon_kg"] * (1 - carbon_cut_pct / 100)
    greener = solve_allocation(dataset, {
        "objective": "cost", "max_carbon_kg": target, "allow_fallback": False
    }, **loader_kwargs)
    return {"status": greener["status"], "baseline": baseline, "greener": greener,
            "target_carbon_kg": round(target, 3),
            "comparison": compare_plans(baseline, greener) if greener.get("validator", {}).get("valid") else None}
