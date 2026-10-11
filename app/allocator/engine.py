"""Whole-order allocation with PuLP and CBC."""

from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date
import hashlib
import json
import math
from pathlib import Path
from time import perf_counter
import uuid

from .candidates import create_candidates, weekly_capacities
from .loader import load_dataset, validate_dataset
from .models import AllocationConfig, Dataset
from .validator import validate_allocation


def _pulp():
    try:
        import pulp
        return pulp
    except ImportError as error:
        raise RuntimeError("Install requirements-allocator.txt, then run scripts/run_allocator.py") from error


def _jsonable(value):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def dataset_signature(data):
    content = asdict(data)
    content.pop("source", None)
    return hashlib.sha256(json.dumps(_jsonable(content), sort_keys=True).encode()).hexdigest()


def _check_config(data, config):
    config.__post_init__()
    for plant in config.excluded_plants:
        if plant not in data.capacities:
            raise ValueError(f"Unknown excluded plant: {plant}")
    for pair in config.excluded_plant_products:
        if tuple(pair) not in data.products:
            raise ValueError(f"Unknown excluded plant/product: {pair}")
    for oid, plant in config.forced_assignments.items():
        if oid not in data.orders or plant not in data.capacities:
            raise ValueError(f"Unknown forced assignment: {oid} -> {plant}")
    for oid, (plant, week) in config.previous_plan.items():
        if (oid not in data.orders or week > data.weeks
                or (plant, data.orders[oid].product) not in data.products):
            raise ValueError(f"Invalid previous assignment for {oid}")
    for oid, (plant, week) in config.frozen_assignments.items():
        if (oid not in data.orders or week > data.weeks
                or (plant, data.orders[oid].product) not in data.products):
            raise ValueError(f"Invalid frozen assignment for {oid}")
        if oid in config.forced_assignments and config.forced_assignments[oid] != plant:
            raise ValueError(f"Conflicting forced and frozen assignment for {oid}")
    for event in config.additional_capacity_events:
        if event.plant not in data.capacities:
            raise ValueError(f"Scenario targets unknown plant {event.plant}")


def _references(data, candidates):
    """Fixed lower bounds; scenario changes do not change the weight scales."""
    by_order = defaultdict(list)
    for candidate in candidates:
        if not candidate.late_days and data.orders[candidate.order_id].qty <= data.capacities[candidate.plant]:
            by_order[candidate.order_id].append(candidate)
    references = {
        "cost": max(1.0, sum(min((c.cost for c in by_order[oid]), default=0) for oid in data.orders)),
        "carbon": max(1.0, sum(min((c.carbon_kg for c in by_order[oid]), default=0) for oid in data.orders)),
    }
    if any(not math.isfinite(value) for value in references.values()):
        raise ValueError("Dataset totals are too large; check the input units")
    return references


def _build_model(data, config, candidates, references, *, best_effort=False):
    pulp = _pulp()
    caps = weekly_capacities(data, use_events=config.use_capacity_events,
                             additional_events=config.additional_capacity_events)
    excluded_pairs = {tuple(pair) for pair in config.excluded_plant_products}
    allowed = [c for c in candidates
               if (best_effort or c.late_days == 0)
               and c.plant not in config.excluded_plants
               and (c.plant, data.orders[c.order_id].product) not in excluded_pairs
               and (c.order_id not in config.forced_assignments or config.forced_assignments[c.order_id] == c.plant)
               and (c.order_id not in config.frozen_assignments
                    or tuple(config.frozen_assignments[c.order_id]) == (c.plant, c.week))
               and caps[c.plant, c.week] >= data.orders[c.order_id].qty]
    model = pulp.LpProblem("MultiPlantAllocation", pulp.LpMinimize)
    x = {c.key: pulp.LpVariable(f"choose_{i}", cat="Binary") for i, c in enumerate(allowed)}
    by_order, by_plant_week = defaultdict(list), defaultdict(list)
    for candidate in allowed:
        by_order[candidate.order_id].append(candidate)
        by_plant_week[candidate.plant, candidate.week].append(candidate)
    u = {oid: pulp.LpVariable(f"unallocated_{i}", cat="Binary")
         for i, oid in enumerate(data.orders)
         if best_effort and oid not in config.forced_assignments and oid not in config.frozen_assignments}

    # Numeric names avoid collisions when input IDs contain punctuation.
    for index, oid in enumerate(data.orders):
        model += pulp.lpSum(x[c.key] for c in by_order[oid]) + u.get(oid, 0) == 1, f"order_{index}"
    for index, (key, cap) in enumerate(caps.items()):
        model += pulp.lpSum(data.orders[c.order_id].qty * x[c.key]
                            for c in by_plant_week[key]) <= cap, f"capacity_{index}"
    cost = pulp.lpSum(c.cost * x[c.key] for c in allowed)
    carbon = pulp.lpSum(c.carbon_kg * x[c.key] for c in allowed)
    if config.max_cost is not None:
        model += cost <= config.max_cost, "cost_budget"
    if config.max_carbon_kg is not None:
        model += carbon <= config.max_carbon_kg, "carbon_budget"
    if config.objective == "cost":
        primary = cost / references["cost"] if config.stability_weight else cost
    elif config.objective == "carbon":
        primary = carbon / references["carbon"] if config.stability_weight else carbon
    else:
        weight_sum = config.cost_weight + config.carbon_weight
        primary = (config.cost_weight / weight_sum) * cost / references["cost"]
        primary += (config.carbon_weight / weight_sum) * carbon / references["carbon"]
    if config.stability_weight and config.previous_plan:
        changes = pulp.lpSum(x[c.key] for c in allowed if c.order_id in config.previous_plan
                             and (c.plant, c.week) != tuple(config.previous_plan[c.order_id]))
        changes += pulp.lpSum(variable for oid, variable in u.items() if oid in config.previous_plan)
        primary += config.stability_weight * changes / len(data.orders)
    impossible = [oid for oid in data.orders if not by_order[oid] and oid not in u]
    return model, x, u, allowed, primary, impossible


def _execute(model, objective, *, seconds):
    pulp = _pulp()
    if seconds <= 0:
        return "Not Solved"
    model.setObjective(objective)
    try:
        model.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=seconds, threads=1, gapRel=0,
                                    options=["randomSeed 1"]))
    except pulp.PulpSolverError as error:
        raise RuntimeError("CBC could not run. Check the PuLP installation and solver executable.") from error
    # CBC can report LpStatusOptimal for an unproven integer incumbent.
    if model.status == pulp.LpStatusOptimal and model.sol_status == pulp.LpSolutionOptimal:
        return "Optimal"
    if model.sol_status == pulp.LpSolutionIntegerFeasible:
        return "Feasible"
    if model.status == pulp.LpStatusOptimal:
        return "Not Solved"
    return pulp.LpStatus[model.status]


def _extract(data, config, x, u, choices, *, status, mode):
    values = [variable.value() for variable in (*x.values(), *u.values())]
    if any(value is None or not math.isfinite(value) or not -1e-6 <= value <= 1 + 1e-6
           or abs(value - round(value)) > 1e-6 for value in values):
        return None
    chosen = [c for c in choices if x[c.key].value() > 0.5]
    missing = sorted(oid for oid, variable in u.items() if variable.value() > 0.5)
    rows = [{
        "order_id": c.order_id, "plant": c.plant, "week": c.week,
        "qty": data.orders[c.order_id].qty, "cost": round(c.cost, 2),
        "carbon_kg": round(c.carbon_kg, 3), "arrival": c.arrival.isoformat(),
        "on_time": c.late_days == 0, "lateness_days": c.late_days,
    } for c in sorted(chosen, key=lambda c: c.order_id)]
    audit = validate_allocation(data, config, rows, missing, strict=(mode == "strict"))
    if not audit["valid"]:
        return None
    unfilled = sum(data.orders[oid].qty for oid in missing)
    return {
        "status": "BEST_EFFORT" if mode == "best_effort" else ("OPTIMAL" if status == "Optimal" else "FEASIBLE"),
        "solver_status": status, "mode": mode, "allocation": rows,
        "unallocated_order_ids": missing, "validator": audit,
        "proven_optimal": status == "Optimal",
        "kpis": {
            "total_cost": audit["total_cost"], "total_carbon_kg": audit["total_carbon_kg"],
            "total_carbon_t": round(audit["raw_total_carbon_kg"] / 1000, 6),
            "late_orders": audit["late_orders"], "total_late_days": audit["total_late_days"],
            "unallocated_orders": len(missing), "unallocated_quantity": unfilled,
            "all_orders_fulfilled": not missing, "all_orders_on_time": audit["delivery_ok"],
            "plant_utilisation": audit["plant_utilisation_by_week"],
        },
    }


def _failure(status, mode, reason):
    return {"status": "INFEASIBLE" if status == "Infeasible" else ("NOT_SOLVED" if status in ("Not Solved", "Feasible") else "ERROR"),
            "solver_status": status, "mode": mode, "allocation": [],
            "unallocated_order_ids": [], "kpis": None, "validator": None,
            "proven_optimal": False, "reason": reason}


def solve_allocation(dataset: Dataset | str | Path, config: AllocationConfig | dict | None = None, **loader_kwargs):
    """Solve strict delivery first; relax it only after proven infeasibility."""
    started = perf_counter()
    data = load_dataset(dataset, **loader_kwargs) if isinstance(dataset, (str, Path)) else deepcopy(dataset)
    validate_dataset(data)
    conf = deepcopy(config) if isinstance(config, AllocationConfig) else AllocationConfig.from_dict(config)
    _check_config(data, conf)
    deadline = started + conf.time_limit_seconds
    candidates = create_candidates(data)
    if any(not math.isfinite(c.cost) or not math.isfinite(c.carbon_kg) for c in candidates):
        raise ValueError("Calculated cost/carbon is too large; check the input units")
    references = _references(data, candidates)
    stages = []

    def run(model, objective, name, impossible=()):
        begin = perf_counter()
        status = "Infeasible" if impossible else _execute(model, objective, seconds=deadline - begin)
        stages.append({"stage": name, "status": status, "seconds": round(perf_counter() - begin, 6)})
        return status

    def finish(result):
        demand = [(oid, asdict(order)) for oid, order in sorted(data.orders.items())]
        signature = hashlib.sha256(json.dumps(_jsonable(demand), sort_keys=True).encode()).hexdigest()
        dataset_hash = dataset_signature(data)
        result.update({
            "run_id": f"al-{uuid.uuid4().hex[:10]}", "dataset_source": data.source,
            "demand_signature": signature, "dataset_signature": dataset_hash,
            "parameters_used": _jsonable(asdict(conf)),
            "objective_references": references,
            "solver_stages": stages, "elapsed_seconds": round(perf_counter() - started, 6),
            "candidate_count": len(candidates),
            "orders_without_on_time_options": strict_impossible,
            "assumptions": [
                "Whole orders; shared plant capacity per week; Sunday production completion.",
                "Arrival = production Sunday + transit days; duty on production + energy + transport.",
                "An overlapping event affects the whole week; the largest loss wins; capacity rounds down.",
                "Balanced weights use per-order on-time cost/carbon lower bounds, fixed across scenarios.",
                "Best effort: fewest unallocated orders, units, late orders, late days, then cost/carbon.",
            ],
            "limitations": [
                "Energy prices, duty base, dispatch timing, and partial-week policy need organizer confirmation.",
                "BEST_EFFORT can include late or unmet orders; it is not a fully on-time allocation.",
                "The time limit is shared across all stages; startup and validation add small overhead.",
            ],
        })
        return result

    model, x, u, choices, primary, impossible = _build_model(data, conf, candidates, references)
    strict_impossible = list(impossible)
    status = run(model, primary, "strict", impossible)
    if status in ("Optimal", "Feasible", "Not Solved"):
        result = _extract(data, conf, x, u, choices, status=status, mode="strict")
        if result is not None:
            return finish(result)
    if status != "Infeasible" or not conf.allow_fallback:
        return finish(_failure(status, "strict", "No valid on-time plan confirmed; see solver_stages"))

    model, x, u, choices, primary, impossible = _build_model(data, conf, candidates, references, best_effort=True)
    pulp = _pulp()
    steps = (
        ("unallocated_count", pulp.lpSum(u.values())),
        ("unallocated_units", pulp.lpSum(data.orders[oid].qty * variable for oid, variable in u.items())),
        ("late_orders", pulp.lpSum(int(c.late_days > 0) * x[c.key] for c in choices)),
        ("late_days", pulp.lpSum(c.late_days * x[c.key] for c in choices)),
        ("final_objective", primary),
    )
    previous = None
    for index, (name, expression) in enumerate(steps):
        status = run(model, expression, name, impossible)
        result = _extract(data, conf, x, u, choices, status=status, mode="best_effort") if status in ("Optimal", "Feasible", "Not Solved") else None
        if status != "Optimal":
            result = result or previous
            if result is None:
                return finish(_failure(status, "best_effort", f"No valid plan confirmed at stage {name}"))
            result.update({"proven_optimal": False, "solver_status": status,
                           "reason": f"Stopped at {name}; remaining priorities are not proven optimal"})
            return finish(result)
        if result is None:
            return finish(_failure("Invalid solution", "best_effort", "Solver output failed independent validation"))
        previous = result
        if index < len(steps) - 1:
            model += expression == round(pulp.value(expression) or 0), f"lock_{name}"
    return finish(previous)


def compare_plans(before, after):
    """Compare verified plans for the same demand."""
    if not (before.get("validator") or {}).get("valid") or not (after.get("validator") or {}).get("valid"):
        raise ValueError("Both plans must pass independent validation")
    if not before.get("demand_signature") or before.get("demand_signature") != after.get("demand_signature"):
        raise ValueError("Cannot compare plans with different orders, quantities, or deadlines")
    a = {row["order_id"]: (row["plant"], row["week"]) for row in before["allocation"]}
    b = {row["order_id"]: (row["plant"], row["week"]) for row in after["allocation"]}
    changed = [oid for oid in sorted(set(a) | set(b)) if a.get(oid) != b.get(oid)]
    same_orders_served = set(a) == set(b)
    return {
        "before_run_id": before["run_id"], "after_run_id": after["run_id"],
        "orders_moved": len(changed), "moved_order_ids": changed,
        "cost_delta": round(after["validator"]["raw_total_cost"] - before["validator"]["raw_total_cost"], 2),
        "carbon_delta_kg": round(after["validator"]["raw_total_carbon_kg"] - before["validator"]["raw_total_carbon_kg"], 3),
        "late_orders_delta": after["kpis"]["late_orders"] - before["kpis"]["late_orders"],
        "unallocated_orders_delta": after["kpis"]["unallocated_orders"] - before["kpis"]["unallocated_orders"],
        "unallocated_quantity_delta": after["kpis"]["unallocated_quantity"] - before["kpis"]["unallocated_quantity"],
        "same_orders_served": same_orders_served,
        "comparison_warning": None if same_orders_served else "Different orders were fulfilled; lower totals may reflect unmet demand, not savings.",
    }


def solve_tradeoff(dataset, carbon_cut_pct=10, *, config=None, time_limit_seconds=None, **loader_kwargs):
    """Minimize cost under a carbon cap derived from the cheapest on-time plan."""
    if type(carbon_cut_pct) not in (int, float) or not math.isfinite(carbon_cut_pct) or not 0 <= carbon_cut_pct <= 100:
        raise ValueError("carbon_cut_pct must be a finite number between 0 and 100")
    data = load_dataset(dataset, **loader_kwargs) if isinstance(dataset, (str, Path)) else dataset
    settings = deepcopy(config) if isinstance(config, AllocationConfig) else AllocationConfig.from_dict(config)
    settings = replace(settings, objective="cost", allow_fallback=False)
    if time_limit_seconds is not None:
        settings = replace(settings, time_limit_seconds=time_limit_seconds)
    baseline = solve_allocation(data, settings)
    if baseline["status"] != "OPTIMAL":
        return {"status": "BASELINE_INFEASIBLE" if baseline["status"] == "INFEASIBLE" else "BASELINE_NOT_OPTIMAL",
                "baseline": baseline}
    target = baseline["validator"]["raw_total_carbon_kg"] * (1 - carbon_cut_pct / 100)
    greener = solve_allocation(data, replace(settings, max_carbon_kg=target))
    return {"status": greener["status"], "baseline": baseline, "greener": greener,
            "target_carbon_kg": target,
            "comparison": compare_plans(baseline, greener) if (greener.get("validator") or {}).get("valid") else None}
