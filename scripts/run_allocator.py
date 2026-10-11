#!/usr/bin/env python3
"""Run allocation, outage comparison, or a carbon-cut scenario."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".allocator-deps"))

from app.allocator import compare_plans, load_dataset, solve_allocation, solve_tradeoff


def print_plan(plan, show_orders):
    print(f"Status: {plan['status']}; solver: {plan['solver_status']}; mode: {plan['mode']}")
    if plan.get("validator"):
        print("Independent validator:", "PASS" if plan["validator"]["valid"] else "FAIL")
        print("KPIs:", json.dumps(plan["kpis"], indent=2))
    if plan.get("reason"):
        print("Reason:", plan["reason"])
    print(f"Elapsed: {plan['elapsed_seconds']:.3f}s; proven optimal: {plan['proven_optimal']}")
    for row in plan["allocation"][:show_orders]:
        print(f"  {row['order_id']} -> {row['plant']} W{row['week']} "
              f"cost={row['cost']:.2f}, carbon={row['carbon_kg']:.3f} kg, on_time={row['on_time']}")
    if plan.get("unallocated_order_ids"):
        print("Unallocated orders:", ", ".join(plan["unallocated_order_ids"][:30]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "synthetic")
    parser.add_argument("--config-file", type=Path, help="JSON settings override objective/weight/budget flags")
    parser.add_argument("--objective", choices=("cost", "carbon", "balanced"), default="cost")
    parser.add_argument("--cost-weight", type=float, default=0.7)
    parser.add_argument("--carbon-weight", type=float, default=0.3)
    parser.add_argument("--carbon-budget-kg", type=float)
    parser.add_argument("--max-cost", type=float)
    parser.add_argument("--start-date", help="Planning Monday, YYYY-MM-DD")
    parser.add_argument("--weeks", type=int)
    parser.add_argument("--energy-price-default", type=float)
    parser.add_argument("--apply-events", action="store_true")
    parser.add_argument("--no-fallback", action="store_true")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--compare", action="store_true", help="Compare baseline and capacity-loss plans")
    modes.add_argument("--carbon-cut-pct", type=float, help="Cost-minimize under a relative carbon cap")
    parser.add_argument("--time-limit", type=float, default=20.0, help="Shared solver time per plan, in seconds")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--show-orders", type=int, default=10)
    args = parser.parse_args()
    if args.show_orders < 0:
        parser.error("--show-orders must be nonnegative")
    try:
        data = load_dataset(args.data_dir, start_date=args.start_date, weeks=args.weeks,
                            energy_price_default=args.energy_price_default)
        config = {
            "objective": args.objective, "cost_weight": args.cost_weight, "carbon_weight": args.carbon_weight,
            "max_carbon_kg": args.carbon_budget_kg, "max_cost": args.max_cost,
            "use_capacity_events": args.apply_events, "allow_fallback": not args.no_fallback,
            "time_limit_seconds": args.time_limit,
        }
        if args.config_file:
            config = json.loads(args.config_file.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                raise ValueError("--config-file must contain a JSON object")
            if args.apply_events:
                config["use_capacity_events"] = True
            if args.no_fallback:
                config["allow_fallback"] = False
        if args.carbon_cut_pct is not None:
            result = solve_tradeoff(data, args.carbon_cut_pct, config=config)
        elif args.compare:
            baseline = solve_allocation(data, {**config, "use_capacity_events": False, "additional_capacity_events": []})
            disrupted = solve_allocation(data, {**config, "use_capacity_events": True})
            result = {"baseline": baseline, "disrupted": disrupted}
            if (baseline.get("validator") or {}).get("valid") and (disrupted.get("validator") or {}).get("valid"):
                result["comparison"] = compare_plans(baseline, disrupted)
        else:
            result = solve_allocation(data, config)
        if args.json_output:
            args.json_output.parent.mkdir(parents=True, exist_ok=True)
            args.json_output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
            print(f"Saved complete JSON: {args.json_output}")
    except (ValueError, OSError, RuntimeError) as error:
        parser.error(str(error))

    plans = [result] if "allocation" in result else [result[key] for key in ("baseline", "disrupted", "greener") if key in result]
    for plan in plans:
        print_plan(plan, args.show_orders)
    if result.get("comparison"):
        print("COMPARISON:", json.dumps(result["comparison"], indent=2))
    return 0 if all((plan.get("validator") or {}).get("valid") for plan in plans) else 1


if __name__ == "__main__":
    raise SystemExit(main())
