#!/usr/bin/env python3
"""Validate CSVs and generated challenge cases."""

import argparse
import json
import math
from datetime import date
from pathlib import Path

from data_schema import REQUIRED_FIELDS, edge_case_audit, read_csv


def check_number(row, field, errors, location, *, integer=False, minimum=0, maximum=None):
    """Check that a number is finite and within its limits."""
    try:
        value = int(row[field]) if integer else float(row[field])
        if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError, OverflowError):
        errors.append(f"{location}: invalid {field}")
        return None


def check_date(value, errors, location):
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        errors.append(f"{location}: invalid date {value!r}")
        return None


def check_unique(rows, fields, errors, table):
    keys = [tuple(row[field] for field in fields) for row in rows]
    if len(set(keys)) != len(keys):
        errors.append(f"{table}: duplicate key {', '.join(fields)}")


def validate_dataset(folder, expected_start=None):
    """Check the files. Supply expected_start if there is no data card."""
    folder = Path(folder)
    errors, warnings, tables = [], [], {}
    card = {}
    card_path = folder / "data_card.json"
    if card_path.exists():
        try:
            card = json.loads(card_path.read_text(encoding="utf-8"))
            if not isinstance(card, dict):
                raise ValueError("Expected a JSON object")
        except (OSError, ValueError) as error:
            errors.append(f"data_card.json: {error}")
            card = {}

    start = expected_start
    if card.get("planning_start") is not None:
        declared_start = check_date(card["planning_start"], errors, "data_card.json planning_start")
        if start is not None and declared_start != start:
            errors.append("Planning start differs from data_card.json")
        if start is None:
            start = declared_start
    if start is None:
        errors.append("Planning start required: use --start-date or a data_card.json planning_start")

    for name, required in REQUIRED_FIELDS.items():
        try:
            rows, columns = read_csv(folder / f"{name}.csv")
            missing = set(required) - columns
            if missing:
                errors.append(f"{name}.csv missing columns: {sorted(missing)}")
            for index, row in enumerate(rows, 2):
                if None in row or any(row.get(field) in (None, "") for field in required):
                    errors.append(f"{name}.csv row {index}: missing value or wrong number of columns")
            tables[name] = rows
        except (OSError, UnicodeError, ValueError) as error:
            errors.append(f"{name}.csv: {error}")
    report = {"valid": False, "errors": errors, "warnings": warnings,
              "rows": {name: len(rows) for name, rows in tables.items()},
              "guide_minimums_met": False}
    if errors:
        return report

    orders = tables["orders"]
    plants = tables["plants"]
    routes = tables["transport_duties"]
    events = tables["capacity_events"]
    plant_ids = {row["plant"] for row in plants}
    destinations = {row["destination"] for row in orders}
    products = {row["product"] for row in plants}
    eligibility = {(row["plant"], row["product"]) for row in plants}
    route_pairs = {(row["plant"], row["destination"]) for row in routes}
    for name, fields in (("orders", ("order_id",)), ("plants", ("plant", "product")),
                         ("transport_duties", ("plant", "destination")), ("capacity_events", ("event_id",))):
        check_unique(tables[name], fields, errors, name)
    for name in ("orders", "plants", "transport_duties"):
        if not tables[name]:
            errors.append(f"{name}.csv must not be empty")

    capacities = {}
    for index, row in enumerate(plants, 2):
        location = f"plants.csv row {index}"
        capacity = check_number(row, "weekly_capacity", errors, location, integer=True, minimum=1)
        if row["plant"] in capacities and capacities[row["plant"]] != capacity:
            errors.append(f"{location}: shared capacity differs across product rows")
        capacities[row["plant"]] = capacity
        for field in ("unit_cost", "energy_kwh_per_unit", "carbon_kg_per_kwh"):
            check_number(row, field, errors, location)
        for field in row:
            if field == "energy_price_per_kwh" or field.startswith("carbon_kg_per_kwh_week_"):
                check_number(row, field, errors, location)

    for index, row in enumerate(orders, 2):
        location = f"orders.csv row {index} ({row['order_id']})"
        check_number(row, "qty", errors, location, integer=True, minimum=1)
        due = check_date(row["due"], errors, location)
        if due is not None and due <= start:
            errors.append(f"{location}: due must be after planning start")
        if row["product"] not in products:
            errors.append(f"{location}: no eligible plant for {row['product']}")

    for plant in sorted(plant_ids):
        for destination in sorted(destinations):
            if (plant, destination) not in route_pairs:
                errors.append(f"Missing transport route for {plant} -> {destination}")
    for index, row in enumerate(routes, 2):
        location = f"transport_duties.csv row {index}"
        if row["plant"] not in plant_ids:
            errors.append(f"{location}: unknown plant {row['plant']}")
        check_number(row, "transit_days", errors, location, integer=True)
        for field in ("transport_cost_per_unit", "duty_pct", "carbon_kg_per_unit"):
            check_number(row, field, errors, location)

    for index, row in enumerate(events, 2):
        location = f"capacity_events.csv row {index} ({row['event_id']})"
        if row["plant"] not in plant_ids:
            errors.append(f"{location}: unknown plant {row['plant']}")
        check_number(row, "capacity_loss_pct", errors, location, maximum=1)
        first = check_date(row["from_date"], errors, location)
        last = check_date(row["to_date"], errors, location)
        if first is not None and last is not None and first > last:
            errors.append(f"{location}: reversed dates")

    if card_path.exists() and report["rows"] != card.get("row_counts"):
        errors.append("CSV row counts disagree with data_card.json")
    if len(orders) < 100:
        warnings.append("Guide minimum is 100 orders; this pack is for debugging")
    if len(destinations) < 8:
        warnings.append("Guide minimum is 8 destinations; this pack is for debugging")
    if plant_ids != {"A", "B", "C"}:
        warnings.append("Official track uses plants A, B, C; this pack is for scalability testing")
    if not events:
        warnings.append("Guide requires at least one capacity-loss event")

    parameters = card.get("parameters", {})
    if not isinstance(parameters, dict):
        errors.append("data_card.json parameters must be an object")
        parameters = {}
    for key, actual in (("orders", len(orders)), ("plants", len(plant_ids)),
                        ("products", len(products)), ("destinations", len(destinations)), ("events", len(events))):
        if key in parameters and parameters[key] != actual:
            errors.append(f"{key} count does not match declared parameters")

    # Audit special cases only in our generated packs.
    if card.get("generator_type") == "flexible" and not errors:
        try:
            weeks = parameters["weeks"]
            if type(weeks) is not int or weeks < 3:
                raise ValueError("weeks must be an integer >= 3")
            if parameters["exclusive_plant"] not in plant_ids:
                raise ValueError("unknown exclusive plant")
            edges = edge_case_audit(orders, plants, routes, events, start,
                                   exclusive_plant=parameters["exclusive_plant"],
                                   exclusive_product=parameters["exclusive_product"], weeks=weeks)
            report["edge_cases"] = edges
            if edges["cheapest_is_late_but_alternative_is_on_time_count"] < 3:
                errors.append("Fewer than 3 cheapest-but-late orders")
            if edges["exclusive_product_eligible_plants"] != [parameters["exclusive_plant"]]:
                errors.append("Exclusive product must have exactly one eligible plant")
            for key in ("duty_reverses_A_B_Germany_P1", "low_carbon_low_capacity_C", "carbon_varies_by_week"):
                if not edges[key]:
                    errors.append(f"Missing expected edge case: {key}")
            if edges["exclusive_product_unallocatable_units_after_event_at_least"] <= 0:
                errors.append("Disruption does not create guaranteed unallocatable demand")
        except (KeyError, ValueError, TypeError) as error:
            errors.append(f"Synthetic edge-case audit: {error}")

    report["guide_minimums_met"] = (
        not errors and len(orders) >= 100 and len(destinations) >= 8
        and plant_ids == {"A", "B", "C"} and bool(events)
        and report.get("edge_cases", {}).get("cheapest_is_late_but_alternative_is_on_time_count", 0) >= 3
    )
    report["valid"] = not errors
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "synthetic")
    parser.add_argument("--start-date", type=date.fromisoformat, help="Required for CSV packs without a data card")
    args = parser.parse_args()
    report = validate_dataset(args.data_dir, args.start_date)
    print(json.dumps(report, indent=2))
    print("VALIDATION:", "PASS" if report["valid"] else "FAIL")
    raise SystemExit(0 if report["valid"] else 1)


if __name__ == "__main__":
    main()
