"""Standalone allocator tests; no LLM. Run with PuLP installed in PYTHONPATH."""

import csv
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".allocator-deps"))
sys.path.insert(0, str(ROOT))

from app.allocator import load_dataset, solve_allocation, solve_tradeoff, compare_plans
from app.allocator.candidates import create_candidates, weekly_capacities
from app.allocator.validator import validate_allocation


def csv_file(folder, name, fields, rows):
    with (Path(folder) / f"{name}.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def fixture(folder, *, second_order_due="2026-10-27"):
    """Tiny two-plant fixture with a clear cost-carbon tradeoff."""
    csv_file(folder, "orders", ["order_id", "product", "qty", "destination", "due"], [
        {"order_id": "O1", "product": "P1", "qty": 6, "destination": "D", "due": "2026-10-20"},
        {"order_id": "O2", "product": "P1", "qty": 6, "destination": "D", "due": second_order_due},
    ])
    csv_file(folder, "plants", ["plant", "product", "weekly_capacity", "unit_cost",
                                 "energy_kwh_per_unit", "carbon_kg_per_kwh", "energy_price_per_kwh"], [
        {"plant": "A", "product": "P1", "weekly_capacity": 6, "unit_cost": 10,
         "energy_kwh_per_unit": 1, "carbon_kg_per_kwh": 5, "energy_price_per_kwh": 2},
        {"plant": "B", "product": "P1", "weekly_capacity": 6, "unit_cost": 20,
         "energy_kwh_per_unit": 1, "carbon_kg_per_kwh": 1, "energy_price_per_kwh": 2},
    ])
    csv_file(folder, "transport_duties", ["plant", "destination", "transport_cost_per_unit",
                                          "transit_days", "duty_pct", "carbon_kg_per_unit"], [
        {"plant": "A", "destination": "D", "transport_cost_per_unit": 1,
         "transit_days": 2, "duty_pct": 0, "carbon_kg_per_unit": 1},
        {"plant": "B", "destination": "D", "transport_cost_per_unit": 1,
         "transit_days": 2, "duty_pct": 0, "carbon_kg_per_unit": 1},
    ])
    csv_file(folder, "capacity_events", ["event_id", "plant", "from_date", "to_date", "capacity_loss_pct"], [
        {"event_id": "E1", "plant": "A", "from_date": "2026-10-12", "to_date": "2026-10-25", "capacity_loss_pct": 1.0},
    ])


class TestAllocator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        fixture(self.tmp.name)
        self.data = load_dataset(self.tmp.name, start_date="2026-10-12", weeks=2)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cost_cheapest_assigns_both_to_A(self):
        result = solve_allocation(self.data, {"objective": "cost"})
        self.assertEqual(result["status"], "OPTIMAL")
        self.assertTrue(result["validator"]["valid"])
        self.assertEqual({x["plant"] for x in result["allocation"]}, {"A"})
        self.assertEqual(result["kpis"]["total_cost"], 156.0)
        self.assertEqual(result["kpis"]["total_carbon_kg"], 72.0)
        self.assertEqual(result["kpis"]["late_orders"], 0)

    def test_carbon_objective_chooses_B(self):
        result = solve_allocation(self.data, {"objective": "carbon"})
        self.assertEqual({x["plant"] for x in result["allocation"]}, {"B"})
        self.assertEqual(result["kpis"]["total_carbon_kg"], 24.0)

    def test_carbon_budget_and_tradeoff(self):
        result = solve_allocation(self.data, {"objective": "cost", "max_carbon_kg": 48})
        self.assertTrue(result["validator"]["valid"])
        self.assertLessEqual(result["kpis"]["total_carbon_kg"], 48)
        self.assertEqual({r["plant"] for r in result["allocation"]}, {"A", "B"})
        analysis = solve_tradeoff(self.data, carbon_cut_pct=50)
        self.assertEqual(analysis["status"], "OPTIMAL")
        self.assertEqual(analysis["target_carbon_kg"], 36)
        self.assertGreater(analysis["greener"]["kpis"]["total_cost"], analysis["baseline"]["kpis"]["total_cost"])

    def test_capacity_events_and_comparison(self):
        baseline = solve_allocation(self.data, {"objective": "cost"})
        after = solve_allocation(self.data, {"objective": "cost", "use_capacity_events": True})
        self.assertTrue(after["validator"]["valid"])
        self.assertEqual({r["plant"] for r in after["allocation"]}, {"B"})
        self.assertEqual(weekly_capacities(self.data, use_events=True)[("A", 1)], 0)
        self.assertEqual(compare_plans(baseline, after)["orders_moved"], 2)

    def test_inline_capacity_event(self):
        after = solve_allocation(self.data, {"objective": "cost", "additional_capacity_events": [
            {"plant": "A", "from_date": "2026-10-12", "to_date": "2026-10-25", "capacity_loss_pct": 1.0}
        ]})
        self.assertTrue(after["validator"]["valid"])
        self.assertEqual({r["plant"] for r in after["allocation"]}, {"B"})
        # Agent/backend handoff is pure JSON, including event dates.
        json.dumps(after)
        self.assertEqual(after["parameters_used"]["additional_capacity_events"][0]["start"], "2026-10-12")

    def test_fallback_reports_late_order_explicitly(self):
        fixture(self.tmp.name, second_order_due="2026-10-20")
        data = load_dataset(self.tmp.name, start_date="2026-10-12", weeks=2)
        # Both orders can fit if they use A and B; exclude B so one must be late.
        result = solve_allocation(data, {"objective": "cost", "excluded_plants": ["B"]})
        self.assertEqual(result["status"], "BEST_EFFORT")
        self.assertTrue(result["validator"]["valid"])
        self.assertEqual(result["kpis"]["late_orders"], 1)
        self.assertEqual(result["kpis"]["unallocated_orders"], 0)

    def test_fallback_unallocated_if_no_capacity(self):
        config = {"objective": "cost", "use_capacity_events": True, "excluded_plants": ["B"]}
        result = solve_allocation(self.data, config)
        self.assertEqual(result["status"], "BEST_EFFORT")
        self.assertTrue(result["validator"]["valid"])
        self.assertEqual(result["kpis"]["unallocated_orders"], 2)
        self.assertEqual(len(result["allocation"]), 0)

    def test_without_fallback_infeasible(self):
        result = solve_allocation(self.data, {
            "use_capacity_events": True, "excluded_plants": ["B"], "allow_fallback": False
        })
        self.assertEqual(result["status"], "INFEASIBLE")
        self.assertEqual(result["allocation"], [])

    def test_validate_catches_capacity_and_forged_cost(self):
        result = solve_allocation(self.data, {"objective": "cost"})
        tampered = [dict(item) for item in result["allocation"]]
        tampered[0]["cost"] = 999999
        from app.allocator.models import AllocationConfig
        audit = validate_allocation(self.data, AllocationConfig(), tampered)
        self.assertFalse(audit["valid"])
        self.assertTrue(any("Cost/carbon" in x for x in audit["errors"]))

    def test_invalid_agent_parameters_rejected(self):
        for config in (
            {"magically_disable_capacity": True},
            {"cost_weight": -0.5},
            {"objective": "hallucinate"},
            {"additional_capacity_events": [{"plant": "NO", "from_date": "2026-10-12",
                                            "to_date": "2026-10-25", "capacity_loss_pct": 0.3}]},
            {"forced_assignments": {"O_NOT_REAL": "A"}},
            {"carbon_weight": "not-a-number"},
            {"time_limit_seconds": float("nan")},
            {"excluded_plant_products": [["A"]]},
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                solve_allocation(self.data, config)

    def test_zero_explicit_energy_price_is_accepted(self):
        with (Path(self.tmp.name) / "plants.csv").open() as f:
            plants = list(csv.DictReader(f))
        for p in plants:
            del p["energy_price_per_kwh"]
        csv_file(self.tmp.name, "plants", list(plants[0]), plants)
        data = load_dataset(self.tmp.name, start_date="2026-10-12", weeks=2, energy_price_default=0)
        self.assertEqual(next(iter(data.products.values())).energy_price, 0)


if __name__ == "__main__":
    unittest.main()
