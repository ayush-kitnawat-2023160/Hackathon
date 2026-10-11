"""Decision checks use fresh solver runs and verified numbers."""

from dataclasses import replace
from datetime import date
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".allocator-deps"))
sys.path.insert(0, str(ROOT / "scripts"))

from app.allocator import (analyze_alternative, analyze_tradeoffs,
                           diagnose_infeasibility, load_dataset, solve_allocation)
from app.allocator.models import AllocationConfig
from app.allocator.validator import validate_allocation
from generate_data import generate_dataset
from test_allocator import fixture


class TestAnalysis(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        fixture(self.temp.name)
        self.data = load_dataset(self.temp.name, start_date="2026-10-12", weeks=2)

    def tearDown(self):
        self.temp.cleanup()

    def test_counterfactual_reoptimizes_and_reports_other_moves(self):
        baseline = solve_allocation(self.data, {"objective": "cost", "allow_fallback": False})
        result = analyze_alternative(self.data, baseline, "O1", "B")
        self.assertEqual(result["status"], "OPTIMAL")
        self.assertTrue(result["both_optimal"])
        self.assertTrue(result["alternative"]["validator"]["valid"])
        self.assertEqual(result["comparison"]["other_orders_moved"], 1)
        self.assertEqual(result["comparison"]["cost_delta"], 60)
        self.assertEqual(analyze_alternative(self.data, baseline, "O1", "A")["status"], "UNCHANGED")

    def test_counterfactual_infeasible_and_rejects_wrong_data(self):
        baseline = solve_allocation(self.data)
        blocked = analyze_alternative(self.data, baseline, "O1", "B")
        self.assertTrue(blocked["comparison"]["same_orders_served"])
        changed = replace(self.data, products={key: replace(value, unit_cost=99)
                                               for key, value in self.data.products.items()})
        with self.assertRaises(ValueError):
            analyze_alternative(changed, baseline, "O1", "B")
        with self.assertRaises(ValueError):
            analyze_alternative(self.data, baseline, "MISSING", "B")

    def test_counterfactual_reuses_recorded_capacity_event(self):
        config = {"additional_capacity_events": [{"plant": "A", "from_date": "2026-10-12",
                                                  "to_date": "2026-10-25", "capacity_loss_pct": 1.0}]}
        baseline = solve_allocation(self.data, config)
        result = analyze_alternative(self.data, baseline, "O1", "A")
        self.assertEqual(result["status"], "INFEASIBLE")
        self.assertIsNone(result["comparison"])
        self.assertEqual(result["alternative"]["parameters_used"]["additional_capacity_events"],
                         baseline["parameters_used"]["additional_capacity_events"])

    def test_tradeoffs_remove_duplicate_points_and_report_infeasibility(self):
        tradeoffs = analyze_tradeoffs(self.data, [0, 25, 50, 75, 100])
        self.assertEqual(tradeoffs["status"], "COMPLETE")
        self.assertEqual([point["status"] for point in tradeoffs["points"]],
                         ["OPTIMAL", "OPTIMAL", "OPTIMAL", "INFEASIBLE", "INFEASIBLE"])
        self.assertEqual(len(tradeoffs["sampled_frontier"]), 3)
        self.assertEqual(tradeoffs["points"][0]["cost"], 156)
        self.assertEqual(tradeoffs["points"][2]["carbon_kg"], 24)
        for targets in ([], [float("nan")], [-1], [101], list(range(9))):
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                analyze_tradeoffs(self.data, targets)

    def test_diagnosis_proves_exclusive_capacity_shortage(self):
        with tempfile.TemporaryDirectory() as folder:
            generate_dataset(folder)
            data = load_dataset(folder)
            result = diagnose_infeasibility(data, {"use_capacity_events": True, "time_limit_seconds": 5})
        shortages = [item for item in result["checks"]
                     if item["kind"] == "single_plant_capacity_shortage" and item["plant"] == "C"]
        self.assertEqual(result["status"], "INFEASIBLE")
        self.assertTrue(result["has_proven_bottleneck"])
        self.assertEqual(shortages[0]["required_units"], 1160)
        self.assertEqual(shortages[0]["available_units"], 944)
        self.assertEqual(shortages[0]["shortage_units_at_least"], 216)
        self.assertFalse(result["complete_explanation"])

    def test_diagnosis_does_not_invent_a_cause(self):
        result = diagnose_infeasibility(self.data, {"max_cost": 1, "allow_fallback": False})
        self.assertEqual(result["status"], "INFEASIBLE")
        self.assertEqual(result["checks"][0]["kind"], "budget_below_lower_bound")
        normal = diagnose_infeasibility(self.data)
        self.assertEqual(normal["status"], "OPTIMAL")
        self.assertFalse(normal["has_proven_bottleneck"])
        with self.assertRaises(ValueError):
            diagnose_infeasibility(self.data, {"use_capacity_events": True}, plan=normal)

    def test_freezing_keeps_plant_and_week(self):
        frozen = {"O1": ["B", 1]}
        result = solve_allocation(self.data, {"frozen_assignments": frozen})
        self.assertTrue(result["validator"]["valid"])
        self.assertEqual(next(row for row in result["allocation"] if row["order_id"] == "O1")["week"], 1)
        self.assertEqual(next(row for row in result["allocation"] if row["order_id"] == "O1")["plant"], "B")
        tampered = [dict(row) for row in result["allocation"]]
        tampered[0]["plant"] = "A"
        audit = validate_allocation(self.data, AllocationConfig.from_dict({"frozen_assignments": frozen}), tampered)
        self.assertFalse(audit["valid"])
        self.assertFalse(audit["eligibility_ok"])
        with self.assertRaises(ValueError):
            solve_allocation(self.data, {"frozen_assignments": {"O1": ["B", 1]},
                                              "forced_assignments": {"O1": "A"}})

    def test_frozen_order_cannot_be_dropped_after_outage(self):
        result = solve_allocation(self.data, {"frozen_assignments": {"O1": ["A", 1]},
                                                   "use_capacity_events": True})
        self.assertEqual(result["status"], "INFEASIBLE")
        self.assertIsNone(result["validator"])
        baseline = solve_allocation(self.data, {"frozen_assignments": {"O1": ["A", 1]}})
        with self.assertRaises(ValueError):
            analyze_alternative(self.data, baseline, "O1", "B")

    def test_dataset_hash_depends_on_contents_not_folder(self):
        result = solve_allocation(self.data)
        copy = replace(self.data, source="another folder")
        self.assertEqual(result["dataset_signature"], solve_allocation(copy)["dataset_signature"])
        changed_routes = dict(self.data.transport)
        changed_routes["A", "D"] = replace(changed_routes["A", "D"], cost=2)
        changed = replace(self.data, transport=changed_routes)
        self.assertNotEqual(result["dataset_signature"], solve_allocation(changed)["dataset_signature"])


if __name__ == "__main__":
    unittest.main()
