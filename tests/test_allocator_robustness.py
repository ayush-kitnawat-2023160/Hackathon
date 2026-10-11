"""Regression checks and a small exhaustive solver comparison."""

from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import itertools
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".allocator-deps"))

from app.allocator import compare_plans, load_dataset, solve_allocation, solve_tradeoff
from app.allocator import engine
from app.allocator.candidates import create_candidates, weekly_capacities
from app.allocator.models import AllocationConfig, CapacityEvent, Dataset, Order, PlantProduct, Transport
from app.allocator.validator import validate_allocation
from test_allocator import fixture, csv_file

START = date(2026, 10, 12)


def small_data(seed):
    rng = random.Random(seed)
    capacities = {plant: rng.randint(3, 8) for plant in "ABC"}
    products = {(plant, "P"): PlantProduct(plant, "P", capacities[plant], rng.randint(1, 12),
                                         2, rng.randint(1, 8), 0.5, {2: rng.randint(1, 8)})
                for plant in "ABC"}
    routes = {(plant, "D"): Transport(plant, "D", 1, rng.randint(1, 6), 0.1, 0.5) for plant in "ABC"}
    orders = {f"O{i}": Order(f"O{i}", "P", rng.randint(2, 5), "D", START + timedelta(days=rng.randint(7, 18)))
              for i in range(3)}
    events = (CapacityEvent("E", "A", START + timedelta(days=7), START + timedelta(days=13), 0.5),)
    return Dataset(orders, products, routes, events, capacities, START, 2, "small-test")


def oracle(data, config, *, best_effort=False):
    """Enumerate all assignments; calculate coefficients directly from inputs."""
    options, references = {}, {"cost": 0.0, "carbon": 0.0}
    capacities = {}
    events = (data.events if config.use_capacity_events else ()) + tuple(config.additional_capacity_events)
    for plant, full in data.capacities.items():
        for week in range(1, data.weeks + 1):
            monday = data.start + timedelta(days=(week - 1) * 7)
            loss = max((e.loss for e in events if e.plant == plant and e.start <= monday + timedelta(days=6) and e.end >= monday), default=0)
            capacities[plant, week] = int(Decimal(full) * (1 - Decimal(str(loss))))
    for oid, order in data.orders.items():
        choices, reference = [], []
        for plant in data.capacities:
            pp = data.products.get((plant, order.product))
            if pp is None:
                continue
            route = data.transport[plant, order.destination]
            cost = order.qty * (pp.unit_cost + pp.energy_kwh * pp.energy_price + route.cost) * (1 + route.duty)
            for week in range(1, data.weeks + 1):
                arrival = data.start + timedelta(days=(week - 1) * 7 + 6 + route.days)
                late = max(0, (arrival - order.due).days)
                carbon = order.qty * (pp.energy_kwh * pp.weekly_carbon.get(week, pp.carbon_factor) + route.carbon)
                item = (plant, week, cost, carbon, late)
                if not late and order.qty <= data.capacities[plant]:
                    reference.append(item)
                if ((late and not best_effort) or plant in config.excluded_plants
                        or (plant, order.product) in config.excluded_plant_products
                        or (oid in config.forced_assignments and config.forced_assignments[oid] != plant)
                        or order.qty > capacities[plant, week]):
                    continue
                choices.append(item)
        references["cost"] += min((item[2] for item in reference), default=0)
        references["carbon"] += min((item[3] for item in reference), default=0)
        if best_effort and oid not in config.forced_assignments:
            choices.append(None)
        options[oid] = choices
    references = {key: max(1, value) for key, value in references.items()}
    best = None
    for combination in itertools.product(*options.values()):
        used, cost, carbon, late_count, late_days, missing, missing_units = {}, 0, 0, 0, 0, 0, 0
        for oid, choice in zip(options, combination):
            if choice is None:
                missing += 1
                missing_units += data.orders[oid].qty
                continue
            plant, week, c, e, late = choice
            used[plant, week] = used.get((plant, week), 0) + data.orders[oid].qty
            cost += c
            carbon += e
            late_count += int(late > 0)
            late_days += late
        if any(qty > capacities[key] for key, qty in used.items()):
            continue
        if config.max_cost is not None and cost > config.max_cost + 1e-8:
            continue
        if config.max_carbon_kg is not None and carbon > config.max_carbon_kg + 1e-8:
            continue
        objective = cost if config.objective == "cost" else carbon
        if config.objective == "balanced":
            total = config.cost_weight + config.carbon_weight
            objective = config.cost_weight / total * cost / references["cost"] + config.carbon_weight / total * carbon / references["carbon"]
        score = (missing, missing_units, late_count, late_days, objective) if best_effort else (objective,)
        if best is None or score < best:
            best = score
    return best


class TestAllocatorRobustness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        fixture(self.tmp.name)
        self.data = load_dataset(self.tmp.name, start_date=START, weeks=2)

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_small_instances(self):
        for seed in range(30):
            for objective in ("cost", "carbon", "balanced"):
                config = AllocationConfig(objective=objective, use_capacity_events=bool(seed % 2),
                                          cost_weight=0.7, carbon_weight=0.3)
                data = small_data(seed)
                with self.subTest(seed=seed, objective=objective):
                    strict = oracle(data, config)
                    expected = strict if strict is not None else oracle(data, config, best_effort=True)
                    result = solve_allocation(data, config)
                    self.assertTrue(result["validator"]["valid"], result)
                    self.assertTrue(result["proven_optimal"], result)
                    cost = result["validator"]["raw_total_cost"]
                    carbon = result["validator"]["raw_total_carbon_kg"]
                    score = cost if objective == "cost" else carbon
                    if objective == "balanced":
                        refs = result["objective_references"]
                        score = 0.7 * cost / refs["cost"] + 0.3 * carbon / refs["carbon"]
                    if strict is None:
                        actual = (result["kpis"]["unallocated_orders"], result["kpis"]["unallocated_quantity"],
                                  result["kpis"]["late_orders"], result["kpis"]["total_late_days"])
                        self.assertEqual(actual, expected[:4])
                    self.assertAlmostEqual(score, expected[-1], places=6)

    def test_capacity_decimal_rounding_and_event_boundaries(self):
        data = replace(self.data, capacities={"A": 100, "B": 100}, weeks=4)
        events = (
            CapacityEvent("E1", "A", START + timedelta(days=7), START + timedelta(days=20), 0.9),
            CapacityEvent("E2", "A", START + timedelta(days=10), START + timedelta(days=15), 0.3),
        )
        caps = weekly_capacities(data, additional_events=events)
        self.assertEqual([caps["A", week] for week in range(1, 5)], [100, 10, 10, 100])
        self.assertEqual([caps["B", week] for week in range(1, 5)], [100] * 4)

    def test_three_cheapest_late_traps(self):
        for days in (4, 5, 6):
            with self.subTest(days=days):
                routes = dict(self.data.transport)
                routes["A", "D"] = replace(routes["A", "D"], days=days)
                result = solve_allocation(replace(self.data, transport=routes))
                o1 = next(row for row in result["allocation"] if row["order_id"] == "O1")
                self.assertEqual(o1["plant"], "B")
                self.assertEqual(result["kpis"]["late_orders"], 0)

    def test_three_capacity_loss_scenarios(self):
        for loss in (0.3, 0.65, 1.0):
            with self.subTest(loss=loss):
                result = solve_allocation(self.data, {"additional_capacity_events": [{
                    "plant": "A", "from_date": "2026-10-12", "to_date": "2026-10-25", "capacity_loss_pct": loss,
                }]})
                self.assertTrue(result["validator"]["valid"])
                self.assertEqual({row["plant"] for row in result["allocation"]}, {"B"})

    def test_three_balanced_weight_settings(self):
        capacities = {plant: 1 for plant in "ABC"}
        products = {(plant, "P"): PlantProduct(plant, "P", 1, cost, 1, carbon, 0)
                    for plant, cost, carbon in (("A", 10, 30), ("B", 20, 15), ("C", 30, 10))}
        routes = {(plant, "D"): Transport(plant, "D", 0, 1, 0, 0) for plant in "ABC"}
        order = Order("O", "P", 1, "D", START + timedelta(days=8))
        data = Dataset({"O": order}, products, routes, (), capacities, START, 1, "weight-test")
        for weights, expected in (((0.9, 0.1), "A"), ((0.5, 0.5), "B"), ((0, 1), "C")):
            with self.subTest(weights=weights):
                result = solve_allocation(data, {"objective": "balanced", "cost_weight": weights[0], "carbon_weight": weights[1]})
                self.assertEqual(result["allocation"][0]["plant"], expected)

    def test_cost_duty_and_weekly_carbon_hand_calculation(self):
        products = dict(self.data.products)
        products["A", "P1"] = replace(products["A", "P1"], weekly_carbon={2: 0.5})
        routes = dict(self.data.transport)
        routes["A", "D"] = replace(routes["A", "D"], duty=0.25)
        candidates = create_candidates(replace(self.data, products=products, transport=routes))
        row = next(c for c in candidates if c.key == ("O2", "A", 2))
        self.assertEqual(row.cost, 6 * (10 + 1 * 2 + 1) * 1.25)
        self.assertEqual(row.carbon_kg, 6 * (1 * 0.5 + 1))
        result = solve_allocation(replace(self.data, products=products, transport=routes), {"objective": "carbon"})
        self.assertEqual(next(row for row in result["allocation"] if row["order_id"] == "O2")["week"], 2)

    def test_ids_with_punctuation_do_not_collide(self):
        orders = {name: replace(order, id=name) for name, order in zip(("order-a", "order+a"), self.data.orders.values())}
        result = solve_allocation(replace(self.data, orders=orders))
        self.assertEqual(result["status"], "OPTIMAL")

    def test_invalid_config_shapes_and_typed_values(self):
        cases = ([], {"excluded_plants": "A"}, {"forced_assignments": []}, {"previous_plan": {"O1": ["A", 1.2]}},
                 {"previous_plan": {"O1": ["A", 0]}}, {"additional_capacity_events": [{}]},
                 {"additional_capacity_events": "bad"}, {"allow_fallback": "false"}, {"max_cost": True},
                 {"excluded_plant_products": [["A", "UNKNOWN"]]}, {"previous_plan": {"UNKNOWN": ["A", 1]}})
        for config in cases:
            with self.subTest(config=config), self.assertRaises(ValueError):
                solve_allocation(self.data, config)
        with self.assertRaises(ValueError):
            AllocationConfig(time_limit_seconds=float("nan"))
        with self.assertRaises(ValueError):
            solve_allocation(replace(self.data, weeks=1.5))

    def test_malformed_rows_and_forged_numbers(self):
        valid = solve_allocation(self.data)["allocation"]
        for field, value in (("week", 1.0), ("qty", True), ("cost", float("nan")), ("on_time", 1),
                             ("carbon_kg", "bad"), ("plant", "UNKNOWN")):
            with self.subTest(field=field):
                rows = deepcopy(valid)
                rows[0][field] = value
                self.assertFalse(validate_allocation(self.data, AllocationConfig(), rows)["valid"])
        self.assertFalse(validate_allocation(self.data, AllocationConfig(), [{}])["valid"])
        self.assertFalse(validate_allocation(self.data, AllocationConfig(), valid, [{}])["valid"])

    def test_validator_rejects_excluded_pair_from_json(self):
        rows = solve_allocation(self.data)["allocation"]
        config = AllocationConfig.from_dict({"excluded_plant_products": [["A", "P1"]]})
        audit = validate_allocation(self.data, config, rows)
        self.assertFalse(audit["valid"])
        self.assertFalse(audit["eligibility_ok"])

    def test_validator_catches_weekly_capacity_overload(self):
        rows = deepcopy(solve_allocation(self.data)["allocation"])
        for row in rows:
            row["week"] = 1
            row["arrival"] = "2026-10-20"
        audit = validate_allocation(self.data, AllocationConfig(), rows)
        self.assertFalse(audit["valid"])
        self.assertFalse(audit["capacity_ok"])

    def test_cost_and_carbon_budgets_stay_hard_in_fallback(self):
        for config in ({"max_cost": 0}, {"max_carbon_kg": 0}):
            result = solve_allocation(self.data, config)
            self.assertEqual(result["status"], "BEST_EFFORT")
            self.assertEqual(result["kpis"]["unallocated_orders"], 2)
            self.assertFalse(result["validator"]["delivery_ok"])

    def test_forced_ineligible_plant_is_infeasible(self):
        products = dict(self.data.products)
        del products["A", "P1"]
        data = replace(self.data, products=products)
        result = solve_allocation(data, {"forced_assignments": {"O1": "A"}})
        self.assertEqual(result["status"], "INFEASIBLE")
        self.assertIsNone(result["kpis"])

    def test_no_false_optimal_claim_after_cbc_timeout(self):
        import pulp
        model = pulp.LpProblem("test")
        with patch.object(model, "solve"):
            model.assignStatus(pulp.LpStatusOptimal, pulp.LpSolutionIntegerFeasible)
            status = engine._execute(model, pulp.LpAffineExpression(), seconds=1)
        self.assertEqual(status, "Feasible")

    def test_timeout_does_not_relax_delivery(self):
        with patch.object(engine, "_execute", return_value="Not Solved") as execute:
            result = solve_allocation(self.data)
        self.assertEqual(result["status"], "NOT_SOLVED")
        self.assertEqual(result["mode"], "strict")
        self.assertEqual(execute.call_count, 1)
        self.assertIsNone(result["kpis"])

    def test_time_limit_is_shared(self):
        real = engine._execute
        limits = []
        def solve(model, objective, *, seconds):
            limits.append(seconds)
            return real(model, objective, seconds=seconds)
        with patch.object(engine, "_execute", side_effect=solve):
            solve_allocation(self.data, {"excluded_plants": ["B"], "use_capacity_events": True, "time_limit_seconds": 2})
        self.assertTrue(all(0 < value <= 2 for value in limits))
        self.assertEqual(limits, sorted(limits, reverse=True))

    def test_timeout_keeps_verified_fallback_incumbent(self):
        real = engine._execute
        calls = 0
        def solve(model, objective, *, seconds):
            nonlocal calls
            calls += 1
            status = real(model, objective, seconds=seconds)
            return "Feasible" if calls == 2 else status
        with patch.object(engine, "_execute", side_effect=solve):
            result = solve_allocation(self.data, {"excluded_plants": ["B"], "use_capacity_events": True})
        self.assertEqual(result["status"], "BEST_EFFORT")
        self.assertFalse(result["proven_optimal"])
        self.assertTrue(result["validator"]["valid"])

    def test_compare_rejects_changed_demand(self):
        before = solve_allocation(self.data)
        orders = dict(self.data.orders)
        orders["O1"] = replace(orders["O1"], qty=5)
        after = solve_allocation(replace(self.data, orders=orders))
        with self.assertRaises(ValueError):
            compare_plans(before, after)

    def test_normalization_and_inputs_stay_fixed_in_scenarios(self):
        original = deepcopy(self.data)
        before = solve_allocation(self.data, {"objective": "balanced"})
        after = solve_allocation(self.data, {"objective": "balanced", "use_capacity_events": True})
        self.assertEqual(before["objective_references"], after["objective_references"])
        self.assertEqual(self.data, original)
        json.dumps(after, allow_nan=False)

    def test_stability_preference_keeps_previous_plant_and_week(self):
        config = {"previous_plan": {"O1": ["B", 1], "O2": ["B", 2]}, "stability_weight": 10}
        result = solve_allocation(self.data, config)
        self.assertEqual({(row["plant"], row["week"]) for row in result["allocation"]}, {("B", 1), ("B", 2)})

    def test_tradeoff_validates_target_and_uses_unrounded_carbon(self):
        for value in (float("nan"), float("inf"), -1, 101, True, "10"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                solve_tradeoff(self.data, value)
        products = {key: replace(pp, carbon_factor=0.123456, energy_kwh=0.123456) for key, pp in self.data.products.items()}
        data = replace(self.data, products=products)
        result = solve_tradeoff(data, 0)
        self.assertEqual(result["status"], "OPTIMAL")
        self.assertEqual(result["target_carbon_kg"], result["baseline"]["validator"]["raw_total_carbon_kg"])

    def test_loader_rejects_bad_rows_and_metadata(self):
        folder = Path(self.tmp.name)
        with self.assertRaises(ValueError):
            load_dataset(folder, start_date=START, weeks=0)
        with self.assertRaises(ValueError):
            load_dataset(folder, start_date=START, weeks=1.5)
        for text in ("[]", "{broken", '{"parameters": []}'):
            (folder / "data_card.json").write_text(text)
            with self.assertRaises(ValueError):
                load_dataset(folder, start_date=START, weeks=2)
        (folder / "data_card.json").unlink()
        with (folder / "plants.csv").open("a") as handle:
            handle.write("A,P2,6,10,1,5,2,EXTRA\n")
        with self.assertRaises(ValueError):
            load_dataset(folder, start_date=START, weeks=2)


if __name__ == "__main__":
    unittest.main()
