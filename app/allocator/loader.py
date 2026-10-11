"""Read the official Annex B CSV schema (plus optional synthetic columns)."""

import csv
import json
import math
import re
from datetime import date
from pathlib import Path

from .models import CapacityEvent, Dataset, Order, PlantProduct, Transport

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
        rows = list(reader)
        for i, row in enumerate(rows, 2):
            if any(row[k] is None or not str(row[k]).strip() for k in REQUIRED[table]):
                raise ValueError(f"{path.name} row {i}: missing value in a required column")
        return rows


def _float(value, name, *, allow_zero=True):
    try:
        number = float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name}: expected a number, got {value!r}") from e
    if not math.isfinite(number) or (number < 0 if allow_zero else number <= 0):
        raise ValueError(f"{name}: expected a finite positive/nonnegative number")
    return number


def _int(value, name, *, allow_zero=False):
    try:
        number = int(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name}: expected integer, got {value!r}") from e
    if number < 0 or (not allow_zero and number == 0):
        raise ValueError(f"{name}: must be positive")
    return number


def load_dataset(folder, *, start_date=None, weeks=None, energy_price_default=None):
    """Load new or generator-produced CSVs; the library never modifies files.

    Explicit energy_price_default is required if the CSV lacks the optional
    energy_price_per_kwh column. Electricity price is NOT in Annex B's minimum.
    """
    folder = Path(folder)
    card_file = folder / "data_card.json"
    card = json.loads(card_file.read_text()) if card_file.exists() else {}
    start = start_date or card.get("planning_start")
    horizon = weeks or card.get("production_weeks") or card.get("parameters", {}).get("weeks")
    if start is None or horizon is None:
        raise ValueError("Provide start_date and weeks (or supply the generator's data_card.json)")
    start = date.fromisoformat(start) if isinstance(start, str) else start
    horizon = _int(horizon, "weeks")
    if start.weekday() != 0:
        raise ValueError("Planning start must be a Monday for the week-end dispatch assumption")

    raw = {name: _read(folder / (name + ".csv"), name) for name in REQUIRED}
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
            if match and value not in (None, ""):
                week_factors[int(match.group(1))] = _float(value, name)
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
    return Dataset(orders, products, routes, tuple(events), capacities, start, horizon, str(folder.resolve()))
