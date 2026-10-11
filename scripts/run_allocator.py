#!/usr/bin/env python3
"""Run the standalone allocation engine; no Planning Agent or LLM required.

Example:
  python3 scripts/run_allocator.py --data-dir data/synthetic
  python3 scripts/run_allocator.py --data-dir data/synthetic --apply-events --compare
  python3 scripts/run_allocator.py --data-dir data/synthetic --objective balanced \
      --cost-weight 0.3 --carbon-weight 0.7
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Local dependency installation works without python3-venv or changing system Python.
sys.path.insert(0, str(ROOT / ".allocator-deps"))

from app.allocator import compare_plans, load_dataset, solve_allocation, solve_tradeoff  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "synthetic")
    parser.add_argument("--config-file", type=Path, help="JSON config from another component (overrides objective/weight/budget CLI options)")
    parser.add_argument("--objective", choices=("cost", "carbon", "balanced"), default="cost")
    parser.add_argument("--cost-weight", type=float, default=0.7)
    parser.add_argument("--carbon-weight", type=float, default=0.3)
    parser.add_argument("--carbon-budget-kg", type=float)
    parser.add_argument("--max-cost", type=float)
    parser.add_argument("--start-date", help="Required if the CSV folder has no data_card.json")
    parser.add_argument("--weeks", type=int, help="Required if the CSV folder has no data_card.json")
    parser.add_argument("--energy-price-default", type=float, help="For official CSVs without energy_price_per_kwh")
    parser.add_argument("--apply-events", action="store_true", help="Apply capacity_events.csv (off by default)")
    parser.add_argument("--no-fallback", action="store_true", help="Return INFEASIBLE rather than best-effort")
    parser.add_argument("--compare", action="store_true", help="Compare normal plan vs plan with capacity events")
    parser.add_argument("--carbon-cut-pct", type=float, help="Compare cheapest baseline with carbon-constrained plan")
    parser.add_argument("--time-limit", type=float, default=20.0)
    parser.add_argument("--json-output", type=Path, help="Write full allocation JSON to this file")
    parser.add_argument("--show-orders", type=int, default=10, help="Display first N allocations")
    args = parser.parse_args()
    loader = {"start_date": args.start_date, "weeks": args.weeks,
              "energy_price_default": args.energy_price_default}
    data = load_dataset(args.data_dir, **loader)
    config = {
        "objective": args.objective,
        "cost_weight": args.cost_weight,
        "carbon_weight": args.carbon_weight,
        "max_carbon_kg": args.carbon_budget_kg,
        "max_cost": args.max_cost,
        "use_capacity_events": args.apply_events,
        "allow_fallback": not args.no_fallback,
        "time_limit_seconds": args.time_limit,
    }
    if args.config_file:
        config = json.loads(args.config_file.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            parser.error("--config-file must contain a JSON object")
        # CLI switches still allow a quick comparison using a teammate's config.
        if args.apply_events:
            config["use_capacity_events"] = True
        if args.no_fallback:
            config["allow_fallback"] = False

    if args.carbon_cut_pct is not None:
        result = solve_tradeoff(data, carbon_cut_pct=args.carbon_cut_pct)
    elif args.compare:
        baseline = solve_allocation(data, {**config, "use_capacity_events": False})
        disrupted = solve_allocation(data, {**config, "use_capacity_events": True})
        result = {"baseline": baseline, "disrupted": disrupted}
        if baseline.get("validator", {}).get("valid") and disrupted.get("validator", {}).get("valid"):
            result["comparison"] = compare_plans(baseline, disrupted)
    else:
        result = solve_allocation(data, config)

    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"Saved complete JSON: {args.json_output}")

    if "allocation" in result:
        print(f"Status: {result['status']}; solver: {result['solver_status']}; mode: {result['mode']}")
        if result.get("validator"):
            print("Independent validator:", "PASS" if result["validator"]["valid"] else "FAIL")
            print("KPIs:", json.dumps(result["kpis"], indent=2))
        if result.get("reason"):
            print("Reason:", result["reason"])
        for row in result["allocation"][:args.show_orders]:
            print(f"  {row['order_id']} -> {row['plant']} W{row['week']}  "
                  f"cost={row['cost']:.2f}, carbon={row['carbon_kg']:.2f} kg, "
                  f"on_time={row['on_time']}")
        if result.get("unallocated_order_ids"):
            print("Unallocated order IDs:", ", ".join(result["unallocated_order_ids"][:30]))
        return

    for label in ("baseline", "disrupted", "greener"):
        plan = result.get(label)
        if plan is not None:
            print(f"{label.upper()} -> status={plan.get('status')} kpis={plan.get('kpis')}")
    if result.get("comparison"):
        print("COMPARISON:", json.dumps(result["comparison"], indent=2))
    else:
        print("Comparison unavailable:", result.get("status", "one plan invalid"))


if __name__ == "__main__":
    main()
