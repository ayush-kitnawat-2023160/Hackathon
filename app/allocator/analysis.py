"""Trade-offs, infeasibility evidence, and alternative decisions."""

from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, replace
import math
from pathlib import Path

from .candidates import create_candidates, weekly_capacities
from .engine import _jsonable, compare_plans, dataset_signature, solve_allocation
from .loader import load_dataset
from .models import AllocationConfig, Dataset
from .validator import validate_allocation


def _data(dataset, loader_kwargs):
    return load_dataset(dataset, **loader_kwargs) if isinstance(dataset, (str, Path)) else deepcopy(dataset)


def _config(config):
    return deepcopy(config) if isinstance(config, AllocationConfig) else AllocationConfig.from_dict(config)


def diagnose_infeasibility(dataset, config=None, *, plan=None, **loader_kwargs):
    """Report necessary failures; leave unexplained interactions as unknown."""
    data = _data(dataset, loader_kwargs)
    settings = AllocationConfig.from_dict(plan["parameters_used"]) if plan is not None and config is None else _config(config)
    if plan is None:
        plan = solve_allocation(data, replace(settings, allow_fallback=False))
    elif plan.get("dataset_signature") != dataset_signature(data):
        raise ValueError("Plan was created from different input data")
    elif plan.get("parameters_used") != _jsonable(asdict(settings)):
        raise ValueError("Plan and diagnostic settings differ")
    if plan.get("validator") and not validate_allocation(
            data, settings, plan.get("allocation", ()), plan.get("unallocated_order_ids", ()),
            strict=plan.get("mode") == "strict")["valid"]:
        raise ValueError("Plan failed independent validation")
    caps = weekly_capacities(data, use_events=settings.use_capacity_events,
                             additional_events=settings.additional_capacity_events)
    excluded_pairs = {tuple(pair) for pair in settings.excluded_plant_products}
    by_order = defaultdict(list)
    for choice in create_candidates(data):
        product = data.orders[choice.order_id].product
        if (choice.late_days == 0 and choice.plant not in settings.excluded_plants
                and (choice.plant, product) not in excluded_pairs
                and (choice.order_id not in settings.forced_assignments
                     or settings.forced_assignments[choice.order_id] == choice.plant)
                and (choice.order_id not in settings.frozen_assignments
                     or tuple(settings.frozen_assignments[choice.order_id]) == (choice.plant, choice.week))
                and data.orders[choice.order_id].qty <= caps[choice.plant, choice.week]):
            by_order[choice.order_id].append(choice)
    checks = []
    unavailable = sorted(oid for oid in data.orders if not by_order[oid])
    if unavailable:
        checks.append({"kind": "no_on_time_option", "order_ids": unavailable,
                       "reason": "No allowed plant/week can make and deliver these whole orders by their due dates."})

    single_plant = defaultdict(list)
    for oid, choices in by_order.items():
        plants = {choice.plant for choice in choices}
        if len(plants) == 1:
            plant = next(iter(plants))
            single_plant[plant].append((oid, data.orders[oid].qty, max(choice.week for choice in choices)))
    for plant, orders in single_plant.items():
        for week in range(1, data.weeks + 1):
            required = sum(qty for _, qty, last_week in orders if last_week <= week)
            available = sum(caps[plant, current] for current in range(1, week + 1))
            if required > available:
                checks.append({"kind": "single_plant_capacity_shortage", "plant": plant,
                               "through_week": week, "required_units": required,
                               "available_units": available, "shortage_units_at_least": required - available,
                               "order_ids": sorted(oid for oid, _, last_week in orders if last_week <= week)})
                break

    if not unavailable:
        for field, limit, metric in (("max_cost", settings.max_cost, "cost"),
                                     ("max_carbon_kg", settings.max_carbon_kg, "carbon_kg")):
            if limit is not None:
                minimum = sum(min(getattr(choice, metric) for choice in by_order[oid]) for oid in data.orders)
                if minimum > limit + 1e-6:
                    checks.append({"kind": "budget_below_lower_bound", "budget": field,
                                   "lower_bound": round(minimum, 6), "limit": limit})
    return {
        "status": plan["status"], "plan_run_id": plan["run_id"],
        "checks": checks, "has_proven_bottleneck": bool(checks),
        "complete_explanation": False,
        "note": "Each reported shortage is a necessary-condition failure; other causes may also exist."
                if checks else "No simple bottleneck found; interacting constraints may still make the plan infeasible.",
    }


def analyze_alternative(dataset, original_plan, order_id, alternative_plant, **loader_kwargs):
    """Force one different plant and let the solver reassign other orders."""
    data = _data(dataset, loader_kwargs)
    if original_plan.get("dataset_signature") != dataset_signature(data):
        raise ValueError("Original plan was created from different input data")
    if order_id not in data.orders or alternative_plant not in data.capacities:
        raise ValueError("Unknown order or alternative plant")
    if original_plan.get("mode") != "strict":
        raise ValueError("Counterfactual requires a fully allocated on-time plan")
    settings = AllocationConfig.from_dict(original_plan.get("parameters_used"))
    audit = validate_allocation(data, settings, original_plan.get("allocation", ()),
                                original_plan.get("unallocated_order_ids", ()), strict=True)
    if not audit["valid"]:
        raise ValueError("Original plan failed independent validation")
    current = next(row for row in original_plan["allocation"] if row["order_id"] == order_id)
    if current["plant"] == alternative_plant:
        return {"status": "UNCHANGED", "order_id": order_id, "current_plant": current["plant"],
                "alternative_plant": alternative_plant, "original_run_id": original_plan["run_id"]}
    if order_id in settings.frozen_assignments:
        raise ValueError("Committed order cannot change plants")
    forced = dict(settings.forced_assignments)
    forced[order_id] = alternative_plant
    alternative = solve_allocation(data, replace(settings, forced_assignments=forced, allow_fallback=False))
    result = {"status": alternative["status"], "order_id": order_id,
              "current_plant": current["plant"], "alternative_plant": alternative_plant,
              "original_run_id": original_plan["run_id"], "alternative": alternative,
              "comparison": None, "both_optimal": False}
    if (alternative.get("validator") or {}).get("valid"):
        comparison = compare_plans(original_plan, alternative)
        comparison["other_orders_moved"] = sum(oid != order_id for oid in comparison["moved_order_ids"])
        result["comparison"] = comparison
        result["both_optimal"] = bool(original_plan.get("proven_optimal") and alternative["proven_optimal"])
    return result


def analyze_tradeoffs(dataset, targets=(0, 5, 10, 20), config=None, **loader_kwargs):
    """Cost-minimize under selected carbon caps; report only proven points as a frontier."""
    if (not isinstance(targets, (list, tuple)) or not targets or len(targets) > 8
            or any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100
                   for value in targets)):
        raise ValueError("targets must contain 1 to 8 percentages from 0 to 100")
    cuts = sorted(set(targets))
    data = _data(dataset, loader_kwargs)
    settings = replace(_config(config), objective="cost", allow_fallback=False)
    baseline = solve_allocation(data, settings)
    result = {"status": "BASELINE_NOT_OPTIMAL", "baseline": baseline,
              "requested_cuts_pct": cuts, "points": [], "sampled_frontier": []}
    if baseline["status"] != "OPTIMAL":
        return result
    base_carbon = baseline["validator"]["raw_total_carbon_kg"]
    base_cost = baseline["validator"]["raw_total_cost"]
    for cut in cuts:
        cap = base_carbon * (1 - cut / 100)
        if settings.max_carbon_kg is not None:
            cap = min(cap, settings.max_carbon_kg)
        plan = baseline if cut == 0 or (base_carbon == 0 and cap == 0) else solve_allocation(
            data, replace(settings, max_carbon_kg=cap))
        point = {"requested_cut_pct": cut, "carbon_cap_kg": cap,
                 "status": plan["status"], "run_id": plan["run_id"],
                 "cost": None, "carbon_kg": None, "actual_cut_pct": None,
                 "extra_cost": None, "extra_cost_pct": None}
        if (plan.get("validator") or {}).get("valid"):
            cost = plan["validator"]["raw_total_cost"]
            carbon = plan["validator"]["raw_total_carbon_kg"]
            point.update({"cost": plan["kpis"]["total_cost"],
                          "carbon_kg": plan["kpis"]["total_carbon_kg"],
                          "actual_cut_pct": round(100 * (base_carbon - carbon) / base_carbon, 3) if base_carbon else 0,
                          "extra_cost": round(cost - base_cost, 2),
                          "extra_cost_pct": round(100 * (cost - base_cost) / base_cost, 3) if base_cost else None})
        result["points"].append(point)
    proven = [point for point in result["points"] if point["status"] == "OPTIMAL"]
    frontier = []
    for point in sorted(proven, key=lambda p: (p["cost"], p["carbon_kg"])):
        if any(other["cost"] <= point["cost"] + 0.01 and other["carbon_kg"] <= point["carbon_kg"] + 0.001
               for other in frontier):
            continue
        frontier.append(point)
    result["sampled_frontier"] = frontier
    result["status"] = "COMPLETE" if all(p["status"] in ("OPTIMAL", "INFEASIBLE") for p in result["points"]) else "PARTIAL"
    return result
