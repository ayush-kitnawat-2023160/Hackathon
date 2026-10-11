"""Read the official Annex B CSV schema (plus optional synthetic columns)."""

import csv
import json
import math
import re
from datetime import date
from pathlib import Path

from .models import CapacityEvent, Dataset, Order, PlantProduct, Transport, check_event

REQUIRED = {
    "orders": {"order_id", "product", "qty", "destination", "due"},
    "plants": {"plant", "product", "weekly_capacity", "unit_cost", "energy_kwh_per_unit", "carbon_kg_per_kwh"},
    "transport_duties": {"plant", "destination", "transport_cost_per_unit", "transit_days", "duty_pct", "carbon_kg_per_unit"},
    "capacity_events": {"event_id", "plant", "from_date", "to_date", "capacity_loss_pct"},
}


def _read(path: Path, table: str):
    if not path.exists():
        if table == "capacity_events":
            return []  # File has not yet arrived at the late data drop.
        raise ValueError(f"Missing mandatory CSV: {path}")
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = REQUIRED[table] - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path.name} is missing columns: {sorted(missing)}")
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError(f"{path.name}: duplicate column names")
        rows = list(reader)
        for i, row in enumerate(rows, 2):
            if None in row:
                raise ValueError(f"{path.name} row {i}: too many columns")
            if any(row[k] is None or not str(row[k]).strip() for k in REQUIRED[table]):
                raise ValueError(f"{path.name} row {i}: missing value in a required column")
        return [{key: value.strip() if isinstance(value, str) else value
                 for key, value in row.items()} for row in rows]


def _float(value, name, *, allow_zero=True):
    try:
        if isinstance(value, bool):
            raise ValueError
        number = float(value)
    except (TypeError, ValueError, OverflowError) as e:
        raise ValueError(f"{name}: expected a number, got {value!r}") from e
    if not math.isfinite(number) or (number < 0 if allow_zero else number <= 0):
        raise ValueError(f"{name}: expected a finite positive/nonnegative number")
    return number


def _int(value, name, *, allow_zero=False):
    try:
        if type(value) is not int and (not isinstance(value, str) or not re.fullmatch(r"[+-]?\d+", value)):
            raise ValueError
        number = int(value)
    except (TypeError, ValueError, OverflowError) as e:
        raise ValueError(f"{name}: expected integer, got {value!r}") from e
    if number < 0 or (not allow_zero and number == 0):
        raise ValueError(f"{name}: must be positive")
    return number


def load_dataset(folder, *, start_date=None, weeks=None, energy_price_default=None):
    """Load CSVs. Missing energy prices need an explicit default."""
    folder = Path(folder)
    card_file = folder / "data_card.json"
    try:
        card = json.loads(card_file.read_text()) if card_file.exists() else {}
    except ValueError as error:
        raise ValueError("Invalid data_card.json") from error
    if not isinstance(card, dict) or not isinstance(card.get("parameters", {}), dict):
        raise ValueError("data_card.json and its parameters must be objects")
    start = start_date if start_date is not None else card.get("planning_start")
    horizon = weeks if weeks is not None else card.get("production_weeks", card.get("parameters", {}).get("weeks"))
    if start is None or horizon is None:
        raise ValueError("Provide start_date and weeks (or supply the generator's data_card.json)")
    start = date.fromisoformat(start) if isinstance(start, str) else start
    horizon = _int(horizon, "weeks")
    if type(start) is not date:
        raise ValueError("Planning start must be a date or YYYY-MM-DD string")
    if start.weekday() != 0:
        raise ValueError("Planning start must be a Monday for the week-end dispatch assumption")

    raw = {name: _read(folder / (name + ".csv"), name) for name in REQUIRED}
    if "row_counts" in card and card["row_counts"] != {name: len(rows) for name, rows in raw.items()}:
        raise ValueError("CSV row counts disagree with data_card.json")
    orders = {}
    for row in raw["orders"]:
        key = row["order_id"]
        if key in orders:
            raise ValueError(f"Duplicate order ID: {key}")
        due = date.fromisoformat(row["due"])
        if due <= start:
            raise ValueError(f"Order {key}: due date must be after planning start")
        orders[key] = Order(key, row["product"], _int(row["qty"], f"{key}.qty"), row["destination"], due)
    if not orders:
        raise ValueError("No orders found")

    products, capacities = {}, {}
    for row in raw["plants"]:
        plant, product = row["plant"], row["product"]
        key = plant, product
        if key in products:
            raise ValueError(f"Duplicate plant/product {key}")
        cap = _int(row["weekly_capacity"], "weekly_capacity")
        if plant in capacities and capacities[plant] != cap:
            raise ValueError(f"Inconsistent SHARED weekly capacity for plant {plant}")
        capacities[plant] = cap
        price_text = row.get("energy_price_per_kwh")
        if price_text in (None, ""):
            price_text = energy_price_default
        if price_text is None:
            raise ValueError(
                "Energy price missing in plants.csv. Supply energy_price_default explicitly; "
                "the official Annex B schema doesn't include electricity price."
            )
        week_factors = {}
        for name, value in row.items():
            match = re.fullmatch(r"carbon_kg_per_kwh_week_(\d+)", name)
            if match:
                week = _int(match.group(1), name)
                if value in (None, ""):
                    raise ValueError(f"{name}: missing carbon factor")
                week_factors[week] = _float(value, name)
        products[key] = PlantProduct(
            plant, product, cap,
            _float(row["unit_cost"], "unit_cost"),
            _float(row["energy_kwh_per_unit"], "energy_kwh_per_unit"),
            _float(row["carbon_kg_per_kwh"], "carbon_kg_per_kwh"),
            _float(price_text, "energy_price_per_kwh"), week_factors,
        )
    if not capacities:
        raise ValueError("No plants found")

    routes = {}
    for row in raw["transport_duties"]:
        key = row["plant"], row["destination"]
        if key in routes:
            raise ValueError(f"Duplicate route {key}")
        if row["plant"] not in capacities:
            raise ValueError(f"Transport route refers to unknown plant {row['plant']}")
        duty = _float(row["duty_pct"], "duty_pct")
        routes[key] = Transport(
            *key,
            _float(row["transport_cost_per_unit"], "transport_cost_per_unit"),
            _int(row["transit_days"], "transit_days", allow_zero=True),
            duty,
            _float(row["carbon_kg_per_unit"], "carbon_kg_per_unit"),
        )
    for order in orders.values():
        if not any((plant, order.product) in products for plant in capacities):
            raise ValueError(f"No eligible plant for order {order.id} product {order.product}")
        for plant in capacities:
            if (plant, order.destination) not in routes:
                raise ValueError(f"Missing transport route {plant} -> {order.destination}")

    events = []
    ids = set()
    for row in raw["capacity_events"]:
        key = row["event_id"]
        if key in ids:
            raise ValueError(f"Duplicate capacity event ID: {key}")
        ids.add(key)
        if row["plant"] not in capacities:
            raise ValueError(f"Capacity event {key} refers to unknown plant {row['plant']}")
        first, last = date.fromisoformat(row["from_date"]), date.fromisoformat(row["to_date"])
        loss = _float(row["capacity_loss_pct"], "capacity_loss_pct")
        if last < first or loss > 1:
            raise ValueError(f"Invalid capacity event {key}")
        events.append(CapacityEvent(key, row["plant"], first, last, loss))
    data = Dataset(orders, products, routes, tuple(events), capacities, start, horizon, str(folder.resolve()))
    validate_dataset(data)
    return data


def validate_dataset(data):
    """Check typed inputs too, before they reach the solver."""
    if not isinstance(data, Dataset):
        raise ValueError("Expected a Dataset or CSV folder")
    if type(data.start) is not date or data.start.weekday() != 0 or type(data.weeks) is not int or data.weeks < 1:
        raise ValueError("Dataset needs a Monday start and positive integer weeks")
    if any(not isinstance(getattr(data, name), dict) for name in ("orders", "products", "transport", "capacities")):
        raise ValueError("Dataset tables must be dictionaries")
    if not data.orders or not data.capacities:
        raise ValueError("Dataset needs orders and plants")
    for plant, capacity in data.capacities.items():
        if not isinstance(plant, str) or not plant.strip() or type(capacity) is not int or capacity < 1:
            raise ValueError("Plant capacities must be positive integers")
    for key, product in data.products.items():
        if (not isinstance(product, PlantProduct) or not isinstance(product.plant, str)
                or not isinstance(product.product, str) or not product.product.strip()):
            raise ValueError("Invalid plant/product record")
        if key != (product.plant, product.product) or product.plant not in data.capacities:
            raise ValueError(f"Invalid plant/product key: {key}")
        if type(product.capacity) is not int or product.capacity != data.capacities[product.plant]:
            raise ValueError(f"Inconsistent shared capacity for plant {product.plant}")
        for name in ("unit_cost", "energy_kwh", "carbon_factor", "energy_price"):
            value = getattr(product, name)
            if type(value) not in (int, float):
                raise ValueError(f"{name} must be numeric")
            _float(value, name)
        if not isinstance(product.weekly_carbon, dict):
            raise ValueError("Weekly carbon factors must be a dictionary")
        for week, factor in product.weekly_carbon.items():
            if type(week) is not int or week < 1:
                raise ValueError("Carbon override weeks must be positive integers")
            if type(factor) not in (int, float):
                raise ValueError("Weekly carbon factors must be numeric")
            _float(factor, "weekly carbon")
    for key, route in data.transport.items():
        if (not isinstance(route, Transport) or not isinstance(route.plant, str)
                or not isinstance(route.destination, str) or not route.destination.strip()):
            raise ValueError("Invalid transport record")
        if key != (route.plant, route.destination) or route.plant not in data.capacities:
            raise ValueError(f"Invalid transport key: {key}")
        if type(route.days) is not int or route.days < 0:
            raise ValueError("Transit days must be nonnegative integers")
        for name in ("cost", "duty", "carbon"):
            value = getattr(route, name)
            if type(value) not in (int, float):
                raise ValueError(f"{name} must be numeric")
            _float(value, name)
    for key, order in data.orders.items():
        if (not isinstance(order, Order) or not isinstance(order.product, str) or not isinstance(order.destination, str)
                or not order.product.strip() or not order.destination.strip()):
            raise ValueError("Invalid order record")
        if key != order.id or not isinstance(key, str) or not key.strip():
            raise ValueError("Invalid order ID")
        if type(order.qty) is not int or order.qty < 1:
            raise ValueError(f"Order {key}: quantity must be a positive integer")
        if type(order.due) is not date or order.due <= data.start:
            raise ValueError(f"Order {key}: due date must be after planning start")
        if not any((plant, order.product) in data.products for plant in data.capacities):
            raise ValueError(f"No eligible plant for order {key}")
        for plant in data.capacities:
            if (plant, order.destination) not in data.transport:
                raise ValueError(f"Missing route {plant} -> {order.destination}")
    event_ids = set()
    for event in data.events:
        if not isinstance(event, CapacityEvent):
            raise ValueError("Invalid capacity event record")
        check_event(event)
        if event.plant not in data.capacities or event.id in event_ids:
            raise ValueError("Unknown plant or duplicate capacity event")
        event_ids.add(event.id)
