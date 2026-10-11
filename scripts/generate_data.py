#!/usr/bin/env python3
"""Generate synthetic multi-plant CSVs. Use --help for options."""

import argparse
import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

from data_schema import DESTINATIONS, REQUIRED_FIELDS, SCHEMA, iso, read_csv, write_csv
from validate_data import validate_dataset


def plant_label(index):
    """Plant labels: A, B, ... Z, AA, AB..."""
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name



def build_plants(plant_ids, prod_ids, cap, p_carbon, default_shape):
    """Plant/product rows share the same plant capacity."""
    last = plant_ids[-1]
    plant_rows = []
    for i, plant in enumerate(plant_ids):
        for j, product in enumerate(prod_ids):
            if product == prod_ids[-1]:
                allowed = plant == last
            elif product == "P1":
                allowed = True
            elif plant in ("A", "B"):
                allowed = True
            elif plant == last:
                allowed = default_shape and product == "P3"
            else:
                allowed = ((i + j) % 3 != 0)
            if not allowed:
                continue

            if plant == "A":
                unit_cost, energy, electricity_price = 58 + j * 5, 4.7 + j * 0.15, 7.2
            elif plant == "B":
                unit_cost, energy, electricity_price = 75 + j * 5, 3.5 + j * 0.15, 8.0
            elif plant == last:
                unit_cost, energy, electricity_price = 103 + j * 3, 2.2 + j * 0.15, 9.5
            else:
                unit_cost, energy, electricity_price = 83 + i * 4 + j * 3, 3.1 + j * 0.12, 8.2

            carbon = p_carbon[plant]
            plant_rows.append({
                "plant": plant, "product": product,
                "weekly_capacity": cap[plant],
                "unit_cost": round(unit_cost, 2),
                "energy_kwh_per_unit": round(energy, 3),
                "carbon_kg_per_kwh": carbon,
                "energy_price_per_kwh": electricity_price,
                "carbon_kg_per_kwh_week_2": round(carbon * 1.12, 4),
                "carbon_kg_per_kwh_week_3": round(carbon * 0.86, 4),
            })

    return plant_rows


def build_transport(rng, plant_ids, markets):
    """Shipping and duty for each plant/destination pair."""
    last = plant_ids[-1]
    transport_rows = []
    for plant in plant_ids:
        for dest in markets:
            if plant == "A":
                transit, shipping, transport_carbon = (2 if dest in ("India", "UAE") else 7), rng.randint(9, 13), 1.7
            elif plant == "B":
                transit, shipping, transport_carbon = (2 if dest in ("India", "UAE") else 3), rng.randint(11, 15), 1.0
            elif plant == last:
                transit, shipping, transport_carbon = (2 if dest in ("Germany", "France") else 3), rng.randint(18, 23), 0.6
            else:
                transit, shipping, transport_carbon = rng.randint(3, 5), rng.randint(17, 23), 0.9
            transport_rows.append({
                "plant": plant, "destination": dest,
                "transport_cost_per_unit": shipping,
                "transit_days": transit,
                "duty_pct": 0.65 if plant == "A" and dest == "Germany" else 0.03,
                "carbon_kg_per_unit": transport_carbon,
            })

    return transport_rows


def build_orders(rng, orders, prod_ids, markets, cap, weeks, start, default_shape):
    """Create special cases and random orders."""
    last = list(cap)[-1]
    destinations = len(markets)
    order_rows = []
    # A is cheaper but late; B arrives on time.
    for idx, dest in enumerate(("France", "Japan", "Brazil"), 1):
        order_rows.append({
            "order_id": f"O-LATE-{idx:03d}", "product": "P1", "qty": 25 + 2 * idx,
            "destination": dest, "due": iso(start + timedelta(days=9)),
        })

    # Demand fits before the outage, but exceeds capacity afterward.
    exclusive_target = round(cap[last] * weeks * (1 - 0.65 / weeks))
    special_count = max(4, round(orders * 0.13), math.ceil(exclusive_target / cap[last]))
    if default_shape:
        exclusive_target, special_count = 1160, 20
    # Split by week so whole orders fit before the outage.
    special_count = max(special_count, weeks)
    weekly_qty, extra_qty = divmod(exclusive_target, weeks)
    weekly_orders, extra_orders = divmod(special_count, weeks)
    for week in range(weeks):
        count = weekly_orders + (week < extra_orders)
        quantity = weekly_qty + (week < extra_qty)
        q, remainder = divmod(quantity, count)
        if q < 1 or quantity > cap[last]:
            raise ValueError("Adjust orders/weeks to fit the exclusive-product stress case")
        for index in range(count):
            order_number = len(order_rows) - 2
            order_rows.append({
                "order_id": (f"O-ONLYC-{order_number:03d}" if default_shape
                             else f"O-ONLY-{order_number:03d}"),
                "product": prod_ids[-1], "qty": q + (index < remainder),
                "destination": "Germany" if order_number % 2 else "France",
                "due": iso(start + timedelta(days=weeks * 7 + 1)),
            })

    regular_count = orders - len(order_rows)
    if regular_count < destinations:
        raise ValueError("Not enough ordinary orders to represent all destinations")
    for idx in range(regular_count):
        product = rng.choice(prod_ids[:-1])
        days = rng.randint(13 if default_shape else 11, weeks * 7 + 3)
        order_rows.append({
            "order_id": f"O-{idx+1:05d}", "product": product,
            "qty": rng.randint(18, 42), "destination": markets[idx % destinations],
            "due": iso(start + timedelta(days=days)),
        })
    rng.shuffle(order_rows)

    return order_rows


def build_events(rng, plant_ids, events, weeks, start, default_shape):
    """Create the two-week outage and any extra events."""
    last = plant_ids[-1]
    event_rows = [{
        "event_id": "EV-C-65PCT-TWO-WEEKS" if default_shape else "EV-EXCLUSIVE-TWO-WEEKS", "plant": last,
        "from_date": iso(start + timedelta(days=7)),
        "to_date": iso(start + timedelta(days=20)),
        "capacity_loss_pct": 0.65,
    }]
    for idx in range(1, events):
        affected_plant = plant_ids[(idx - 1) % (len(plant_ids) - 1)]
        week = 1 + ((idx - 1) % (weeks - 1))
        event_rows.append({
            "event_id": f"EV-{idx:03d}", "plant": affected_plant,
            "from_date": iso(start + timedelta(days=week * 7)),
            "to_date": iso(start + timedelta(days=week * 7 + 6)),
            "capacity_loss_pct": round(rng.uniform(0.2, 0.5), 2),
        })

    return event_rows


def build_tables(*, seed, orders, plants, products, destinations, events, weeks, start):
    """Set up plants and build the four tables."""
    if plants < 3:
        raise ValueError("Need >=3 plants so A, B and the exclusive-product plant exist")
    if products < 3:
        raise ValueError("Need >=3 products (including one exclusive-to-last-plant product)")
    if destinations < 4:
        raise ValueError("Need >=4 destinations for deadline and import-duty edge cases")
    if orders < max(12, destinations + 7):
        raise ValueError("Need >=max(12, destinations + 7) orders to exercise all destinations/traps")
    if events < 1:
        raise ValueError("Need >=1 capacity event to test re-planning")
    if weeks < 3:
        raise ValueError("Need >=3 weeks to include a 2-week disruption")
    if start.weekday() != 0:
        raise ValueError("Planning start must be Monday")

    rng = random.Random(seed)

    plant_ids = [plant_label(i) for i in range(plants)]
    last = plant_ids[-1]
    default_shape = (orders, plants, products, destinations, weeks) == (150, 3, 4, 12, 4)
    prod_ids = [f"P{i}" for i in range(1, products)] + ["P_ONLY_C" if default_shape else "P_EXCLUSIVE"]
    markets = list(DESTINATIONS[:destinations])
    while len(markets) < destinations:
        markets.append(f"MARKET_{len(markets) + 1:03d}")

    # Scale capacity with demand; keep the last plant smaller.
    typical_capacity = max(500, math.ceil(orders * 35 * 1.7 / (weeks * (plants - 1))))
    special_capacity = min(max(140, round(350 * orders / 150)), int(typical_capacity * 0.6))
    cap = {p: typical_capacity + ((i % 3) * 30) for i, p in enumerate(plant_ids)}
    cap[last] = special_capacity
    if default_shape:
        cap = {"A": 840, "B": 760, "C": 350}

    # A has high carbon; the last plant has low carbon.
    p_carbon = {p: round(max(0.25, 0.45 - 0.025 * max(0, i - 1)), 4)
                for i, p in enumerate(plant_ids)}
    p_carbon["A"] = 0.82
    p_carbon["B"] = 0.45
    p_carbon[last] = 0.16

    plant_rows = build_plants(plant_ids, prod_ids, cap, p_carbon, default_shape)
    transport_rows = build_transport(rng, plant_ids, markets)
    order_rows = build_orders(rng, orders, prod_ids, markets, cap, weeks, start, default_shape)
    event_rows = build_events(rng, plant_ids, events, weeks, start, default_shape)

    return {
        "orders": order_rows,
        "plants": plant_rows,
        "transport_duties": transport_rows,
        "capacity_events": event_rows,
    }, last, prod_ids[-1]


def generate_dataset(output, seed=20261011, order_count=150, start=date(2026, 10, 12),
                     *, plants=3, products=4, destinations=12, events=1, weeks=4):
    """Write the dataset and return its validation report."""
    orders = order_count
    tables, last, exclusive_product = build_tables(
        seed=seed, orders=orders, plants=plants, products=products,
        destinations=destinations, events=events, weeks=weeks, start=start,
    )
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        write_csv(output / f"{name}.csv", SCHEMA[name], rows)

    parameters = {
        "orders": orders, "plants": plants, "products": products,
        "destinations": destinations, "events": events, "weeks": weeks,
        "exclusive_plant": last, "exclusive_product": exclusive_product,
    }
    card = {
        "name": "HCL Multi-Plant VARIABLE SYNTHETIC stress dataset",
        "version": "3.0", "generator_type": "flexible", "parameters": parameters,
        "generated_by": "scripts/generate_data.py",
        "generator_seed": seed, "planning_start": iso(start),
        "row_counts": {name: len(rows) for name, rows in tables.items()},
        "date_range": {
            "earliest_order_due": min(o["due"] for o in tables["orders"]),
            "latest_order_due": max(o["due"] for o in tables["orders"]),
        },
        "synthetic_only": True,
        "privacy": "Procedural synthetic data; no real company or personal data.",
        "generation_method": "Deterministic Python PRNG; no LLM generates the data rows.",
        "ai_usage": "Generator reviewed and refactored with a Codex coding assistant; checked with unittest and CLI validation.",
        "units": {
            "cost": "arbitrary synthetic currency units", "capacity": "units per plant-week",
            "carbon_kg_per_kwh": "kg CO2e per kWh",
            "carbon_kg_per_unit": "kg CO2e per shipped unit",
            "percentages": "fractions; 0.65 = 65%",
        },
        "deliberate_edge_cases": [
            "3 named orders where cheapest first-week option is late",
            "one product manufactured only by the last (lowest-carbon) plant",
            "Germany import duty reverses cheapest A-versus-B route for P1",
            "last plant has lowest emissions but lowest capacity",
            "2-week 65% loss at last plant makes exclusive demand exceed available capacity",
            "carbon intensity differs for production weeks 2 and 3",
        ],
        "validation_checks": [
            "Required Annex B columns; uniqueness and coverage; positive values and dates",
            "Reproducible edge-case audit; row counts; exclusive-product disruption shortage",
        ],
        "known_limitations": [
            "HCL official problem specifies exactly three plants; other values are scalability testing only.",
            "Shared plant weekly capacity repeated on each product row; do not sum across products.",
            "Production assumed complete each Sunday; arrival Sunday + route transit_days.",
            "Energy price is an optional extra column not in the minimum schema.",
            "Duty convention (production + energy + freight base) requires organizer confirmation.",
            "Generator ensures stress-case consistency, not optimal or baseline feasible allocation for every dimension.",
        ],
    }
    card["production_weeks"] = weeks
    (output / "data_card.json").write_text(json.dumps(card, indent=2) + "\n", encoding="utf-8")
    report = validate_dataset(output)
    card["checks_run"] = report
    (output / "data_card.json").write_text(json.dumps(card, indent=2) + "\n", encoding="utf-8")
    (output / "validation_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if not report["valid"]:
        raise ValueError("Generated dataset failed validation: " + "; ".join(report["errors"]))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None, help="Output directory (defaults: data/synthetic or data/synthetic_custom)")
    parser.add_argument("--orders", type=int, default=150)
    parser.add_argument("--plants", type=int, default=3, help="Plant count; >3 is a scalability test, not the exact HCL track")
    parser.add_argument("--products", type=int, default=4, help="Total product types, including one exclusive product")
    parser.add_argument("--destinations", type=int, default=12, help="Number of distinct destinations")
    parser.add_argument("--events", type=int, default=1, help="Number of capacity-loss events")
    parser.add_argument("--weeks", type=int, default=4, help="Number of production weeks")
    parser.add_argument("--seed", type=int, default=20261011)
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2026, 10, 12))
    args = parser.parse_args()
    is_flexible = (args.orders, args.plants, args.products, args.destinations, args.events, args.weeks) != (150, 3, 4, 12, 1, 4)
    if args.output is None:
        args.output = Path(__file__).resolve().parents[1] / "data" / ("synthetic_custom" if is_flexible else "synthetic")
    try:
        report = generate_dataset(
            args.output, seed=args.seed, order_count=args.orders, start=args.start_date,
            plants=args.plants, products=args.products, destinations=args.destinations,
            events=args.events, weeks=args.weeks,
        )
    except ValueError as error:
        parser.error(str(error))
    print("SYNTHETIC HCLTech Annex B dataset generated:", args.output.resolve())
    for name, count in report["rows"].items():
        print(f"  {name}.csv: {count} rows")
    for warning in report["warnings"]:
        print(f"  WARNING: {warning}")
    print("  Edge case audit:")
    for name, value in report["edge_cases"].items():
        print(f"    {name}: {value}")
    print("VALIDATION: PASS")


if __name__ == "__main__":
    main()
