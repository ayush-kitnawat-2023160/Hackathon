#!/usr/bin/env python3
"""Run tests and benchmark cost, carbon, balanced, and outage plans."""

import argparse
from collections import Counter
import hashlib
import json
import math
import platform
from pathlib import Path
import statistics
import sys
from time import perf_counter
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".allocator-deps"))
sys.path.insert(0, str(ROOT / "scripts"))

from app.allocator import load_dataset, solve_allocation
from app.allocator.candidates import create_candidates, weekly_capacities
from app.allocator.models import AllocationConfig
from app.allocator.validator import validate_allocation
from generate_data import generate_dataset


def greedy_baseline(data):
    remaining = weekly_capacities(data)
    options = {}
    for candidate in create_candidates(data):
        if not candidate.late_days:
            options.setdefault(candidate.order_id, []).append(candidate)
    orders = sorted(data.orders.values(), key=lambda order: (
        sum((plant, order.product) in data.products for plant in data.capacities) != 1, order.due, order.id))
    rows, missing = [], []
    for order in orders:
        choices = sorted(options.get(order.id, ()), key=lambda c: (c.cost, c.week, c.plant))
        chosen = next((c for c in choices if remaining[c.plant, c.week] >= order.qty), None)
        if chosen is None:
            missing.append(order.id)
            continue
        remaining[chosen.plant, chosen.week] -= order.qty
        rows.append({"order_id": order.id, "plant": chosen.plant, "week": chosen.week, "qty": order.qty,
                     "cost": round(chosen.cost, 2), "carbon_kg": round(chosen.carbon_kg, 3),
                     "arrival": chosen.arrival.isoformat(), "on_time": True, "lateness_days": 0})
    return validate_allocation(data, AllocationConfig(), rows, missing, strict=False)


class Results(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.append(test.id())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "eval" / "allocator_report.json")
    parser.add_argument("--time-limit", type=float, default=10)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    AllocationConfig(time_limit_seconds=args.time_limit)
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    tests = unittest.TextTestRunner(verbosity=1, resultclass=Results).run(suite)
    data_dir = ROOT / "data" / "evaluation_default"
    generate_dataset(data_dir)
    data = load_dataset(data_dir)
    greedy = greedy_baseline(data)
    records = []
    for repeat in range(args.repeats):
        for label, settings in (("cost", {}), ("carbon", {"objective": "carbon"}),
                                ("balanced", {"objective": "balanced"}), ("outage", {"use_capacity_events": True})):
            started = perf_counter()
            result = solve_allocation(data, {**settings, "time_limit_seconds": args.time_limit})
            audit = result.get("validator") or {}
            records.append({"case": label, "repeat": repeat + 1, "status": result["status"],
                            "valid": audit.get("valid", False), "proven_optimal": result["proven_optimal"],
                            "seconds": round(perf_counter() - started, 6), "kpis": result.get("kpis"),
                            "solver_stages": result["solver_stages"]})
    times = sorted(record["seconds"] for record in records)
    code_files = sorted((ROOT / "app" / "allocator").glob("*.py"))
    code_hash = hashlib.sha256(b"".join(path.name.encode() + path.read_bytes() for path in code_files)).hexdigest()
    report = {
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "pulp": __import__("pulp").__version__, "allocator_sha256": code_hash},
        "scope": "Standalone allocator only; agent faithfulness/API/Docker evaluation is still separate.",
        "method": "Unit assertions, hand-calculated coefficients, 90 exhaustive small-instance objective comparisons, seeded full-pack benchmarks.",
        "tests": {"run": tests.testsRun, "passed": tests.passed,
                  "failures": [test.id() for test, _ in tests.failures], "errors": [test.id() for test, _ in tests.errors]},
        "benchmark": {"orders": len(data.orders), "plants": len(data.capacities), "seed": 20261011,
                      "repeats": args.repeats, "time_limit_per_plan_seconds": args.time_limit,
                      "p50_seconds": statistics.median(times), "p95_seconds": times[math.ceil(len(times) * 0.95) - 1],
                      "validity_rate": sum(record["valid"] for record in records) / len(records),
                      "statuses": dict(Counter(record["status"] for record in records)), "runs": records},
        "greedy_baseline": greedy,
        "interpretation": [
            "Optimal means CBC proved optimality and the independent validator passed.",
            "Feasible means a verified incumbent; optimality was not proven.",
            "Best effort may leave demand unmet; lower totals are not automatically savings.",
            "Performance is specific to this dataset and machine; small-instance exact checks do not prove every future input correct.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(f"Report: {args.output}")
    print(f"Tests: {tests.testsRun}; benchmark validity: {report['benchmark']['validity_rate']:.0%}")
    return 0 if tests.wasSuccessful() and all(record["valid"] for record in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
