"""Run with python3 -m unittest discover -s tests -v."""

import csv
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from generate_data import (  # noqa: E402
    REQUIRED_FIELDS,
    generate_dataset,
    read_csv,
    validate_dataset,
)
from flexible_data import generate_flexible_dataset  # noqa: E402


class TestSyntheticGenerator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.start = date(2026, 10, 12)
        self.report = generate_dataset(self.folder, order_count=150, seed=20261011, start=self.start)

    def tearDown(self):
        self.tmp.cleanup()

    def test_required_columns_and_counts(self):
        self.assertTrue(self.report["valid"])
        self.assertEqual(self.report["rows"], {
            "orders": 150, "plants": 9, "transport_duties": 36, "capacity_events": 1
        })
        for table, mandatory in REQUIRED_FIELDS.items():
            _, fields = read_csv(self.folder / f"{table}.csv")
            self.assertTrue(set(mandatory).issubset(fields), table)

    def test_all_required_traps_are_present(self):
        e = self.report["edge_cases"]
        self.assertGreaterEqual(e["cheapest_is_late_but_alternative_is_on_time_count"], 3)
        self.assertEqual(e["exclusive_product_eligible_plants"], ["C"])
        self.assertGreater(e["exclusive_product_required_units"], e["exclusive_product_4week_disrupted_capacity"])
        self.assertLessEqual(e["exclusive_product_required_units"], e["exclusive_product_4week_baseline_capacity"])
        self.assertTrue(e["duty_reverses_A_B_Germany_P1"])
        self.assertTrue(e["low_carbon_low_capacity_C"])
        self.assertTrue(e["carbon_varies_by_week"])

    def test_two_week_event_and_fractional_percentage(self):
        events, _ = read_csv(self.folder / "capacity_events.csv")
        event = events[0]
        self.assertEqual((date.fromisoformat(event["to_date"]) - date.fromisoformat(event["from_date"])).days, 13)
        self.assertEqual(float(event["capacity_loss_pct"]), 0.65)

    def test_same_seed_generates_identical_csv(self):
        with tempfile.TemporaryDirectory() as other:
            generate_dataset(other, order_count=150, seed=20261011, start=self.start)
            for table in REQUIRED_FIELDS:
                self.assertEqual(
                    (self.folder / f"{table}.csv").read_bytes(),
                    (Path(other) / f"{table}.csv").read_bytes(),
                )

    def test_bad_csv_is_rejected(self):
        path = self.folder / "orders.csv"
        with path.open(newline="") as file:
            reader = csv.DictReader(file)
            rows = list(reader)
            fields = list(reader.fieldnames)
        fields.remove("due")
        with path.open("w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        self.assertFalse(validate_dataset(self.folder, self.start)["valid"])

    def test_disclosed_data_card_matches_files(self):
        card = json.loads((self.folder / "data_card.json").read_text())
        self.assertTrue(card["synthetic_only"])
        self.assertEqual(card["row_counts"], self.report["rows"])
        self.assertEqual(card["generator_seed"], 20261011)

    def change_cell(self, table, field, value):
        path = self.folder / f"{table}.csv"
        rows, _ = read_csv(path)
        fields = list(rows[0])
        rows[0][field] = value
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def test_invalid_values_are_reported_without_crashing(self):
        cases = [
            ("orders", "due", "not-a-date"),
            ("orders", "due", "2026-10-12"),
            ("orders", "qty", "2.5"),
            ("plants", "unit_cost", "nan"),
            ("plants", "energy_price_per_kwh", "-1"),
            ("plants", "carbon_kg_per_kwh_week_2", "inf"),
            ("transport_duties", "transit_days", "-1"),
            ("transport_duties", "carbon_kg_per_unit", "nan"),
            ("capacity_events", "plant", "UNKNOWN"),
            ("capacity_events", "capacity_loss_pct", "1.01"),
            ("capacity_events", "from_date", "2027-01-01"),
        ]
        for table, field, value in cases:
            with self.subTest(table=table, field=field, value=value):
                generate_dataset(self.folder)
                self.change_cell(table, field, value)
                report = validate_dataset(self.folder)
                self.assertFalse(report["valid"])
                self.assertTrue(report["errors"])

    def test_missing_route_and_duplicate_event_are_rejected(self):
        path = self.folder / "transport_duties.csv"
        rows, _ = read_csv(path)
        from generate_data import write_csv
        write_csv(path, list(rows[0]), rows[1:])
        self.assertFalse(validate_dataset(self.folder)["valid"])
        generate_dataset(self.folder)
        rows, _ = read_csv(self.folder / "capacity_events.csv")
        write_csv(self.folder / "capacity_events.csv", list(rows[0]), rows * 2)
        self.assertFalse(validate_dataset(self.folder)["valid"])

    def test_corrupt_card_is_rejected(self):
        for value in ("{broken", "[]", '{"parameters": []}'):
            with self.subTest(value=value):
                (self.folder / "data_card.json").write_text(value)
                self.assertFalse(validate_dataset(self.folder, self.start)["valid"])

    def test_planning_start_is_read_from_card(self):
        generate_dataset(self.folder, start=date(2026, 11, 2))
        self.assertTrue(validate_dataset(self.folder)["valid"])
        self.assertFalse(validate_dataset(self.folder, self.start)["valid"])

    def test_annex_b_without_optional_columns_or_card(self):
        from generate_data import write_csv
        rows, _ = read_csv(self.folder / "plants.csv")
        write_csv(self.folder / "plants.csv", REQUIRED_FIELDS["plants"],
                  [{key: row[key] for key in REQUIRED_FIELDS["plants"]} for row in rows])
        (self.folder / "data_card.json").unlink()
        self.assertTrue(validate_dataset(self.folder, self.start)["valid"])
        self.assertFalse(validate_dataset(self.folder)["valid"])

    def test_default_baseline_has_a_constructive_on_time_plan(self):
        # Check baseline feasibility with a simple schedule.
        orders, _ = read_csv(self.folder / "orders.csv")
        plants, _ = read_csv(self.folder / "plants.csv")
        routes, _ = read_csv(self.folder / "transport_duties.csv")
        eligibility = {(p["plant"], p["product"]) for p in plants}
        transit = {(r["plant"], r["destination"]): int(r["transit_days"]) for r in routes}
        capacity = {p["plant"]: int(p["weekly_capacity"]) for p in plants}
        remaining = {(plant, week): cap for plant, cap in capacity.items() for week in range(4)}
        orders.sort(key=lambda o: (o["product"] != "P_ONLY_C", o["due"], -int(o["qty"])))
        for order in orders:
            options = []
            for plant in capacity:
                if (plant, order["product"]) not in eligibility:
                    continue
                for week in range(4):
                    arrival = self.start + timedelta(days=week * 7 + 6 + transit[plant, order["destination"]])
                    if arrival <= date.fromisoformat(order["due"]) and remaining[plant, week] >= int(order["qty"]):
                        options.append((week, -remaining[plant, week], plant))
            self.assertTrue(options, order["order_id"])
            week, _, plant = min(options)
            remaining[plant, week] -= int(order["qty"])


class TestFlexibleDataGenerator(unittest.TestCase):
    def test_five_plants_five_hundred_orders(self):
        with tempfile.TemporaryDirectory() as folder:
            r = generate_flexible_dataset(
                folder, orders=500, plants=5, products=6, destinations=18,
                events=4, weeks=4, seed=987, start=date(2026, 10, 12),
            )
            self.assertTrue(r["valid"])
            self.assertEqual(r["rows"]["orders"], 500)
            self.assertEqual(r["rows"]["transport_duties"], 90)
            self.assertEqual(r["rows"]["capacity_events"], 4)
            self.assertEqual(r["edge_cases"]["exclusive_plant"], "E")
            self.assertGreater(r["edge_cases"]["exclusive_product_unallocatable_units_after_event_at_least"], 0)
            self.assertFalse(r["guide_minimums_met"])
            self.assertTrue(validate_dataset(folder, date(2026, 10, 12))["valid"])

    def test_variable_products_destinations_weeks(self):
        with tempfile.TemporaryDirectory() as folder:
            r = generate_flexible_dataset(
                folder, orders=120, plants=3, products=8, destinations=9,
                events=2, weeks=6, seed=23, start=date(2026, 10, 12),
            )
            self.assertTrue(r["valid"])
            self.assertEqual(r["rows"]["orders"], 120)
            self.assertEqual(r["rows"]["transport_duties"], 27)
            self.assertEqual(r["edge_cases"]["exclusive_plant"], "C")
            self.assertNotIn("exclusive_product_4week_baseline_capacity", r["edge_cases"])
            self.assertTrue(r["guide_minimums_met"])

    def test_small_dataset_warns_below_guide_minimum(self):
        with tempfile.TemporaryDirectory() as folder:
            r = generate_flexible_dataset(
                folder, orders=40, plants=4, products=3, destinations=6,
                events=1, weeks=4, seed=42, start=date(2026, 10, 12),
            )
            self.assertTrue(r["valid"])
            self.assertFalse(r["guide_minimums_met"])
            self.assertTrue(r["warnings"])

    def test_second_generator_run_is_reproducible(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            kw = dict(orders=200, plants=4, products=5, destinations=10,
                      events=3, weeks=5, seed=555, start=date(2026, 10, 12))
            generate_flexible_dataset(a, **kw)
            generate_flexible_dataset(b, **kw)
            for t in REQUIRED_FIELDS:
                self.assertEqual((Path(a) / f"{t}.csv").read_bytes(),
                                 (Path(b) / f"{t}.csv").read_bytes())

    def test_wrong_dimensions_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                generate_flexible_dataset(
                    folder, orders=20, plants=2, products=3, destinations=8,
                    events=1, weeks=4, start=date(2026, 10, 12),
                )


if __name__ == "__main__":
    unittest.main()
