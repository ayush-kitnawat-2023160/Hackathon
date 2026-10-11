"""Strict, transactional CSV acceptance for the four Annex B tables."""

import csv
import io
import math
import tempfile
from dataclasses import replace
from datetime import date
from pathlib import Path

from app.allocator.loader import REQUIRED, load_dataset

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_ROWS = 10000
REQUIRED_TO_SOLVE = {"orders", "plants", "transport_duties"}


def _number(value, name, *, integer=False, positive=False, fraction=False):
    try:
        result = int(value) if integer else float(value)
        if integer and str(result) != str(value).strip():
            raise ValueError
    except (ValueError, TypeError) as e:
        raise ValueError(f"{name} must be a {'positive integer' if integer else 'number'}") from e
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative and finite'}")
    if fraction and result > 1:
        raise ValueError(f"{name} must be a fraction between 0 and 1")
    return result


def validate_upload(table, content):
    if table not in REQUIRED:
        raise ValueError(f"Unsupported table: {table}")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("CSV exceeds the 2 MiB file size limit")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ValueError("CSV must be UTF-8 encoded") from e
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        fields = reader.fieldnames or []
        if len(set(fields)) != len(fields) or not REQUIRED[table] <= set(fields):
            raise ValueError(f"Missing or duplicate columns; required: {sorted(REQUIRED[table])}")
        rows = list(reader)
        if len(rows) > MAX_ROWS:
            raise ValueError("CSV exceeds the 10000 row limit")
        for number, row in enumerate(rows, 2):
            if None in row or any(not str(row.get(col) or "").strip() for col in REQUIRED[table]):
                raise ValueError(f"Invalid or missing value on row {number}")
        if table != "capacity_events" and not rows:
            raise ValueError(f"{table} cannot be empty")
        keys = {
            "orders": ("order_id",), "plants": ("plant", "product"),
            "transport_duties": ("plant", "destination"),
            "capacity_events": ("event_id",),
        }[table]
        seen = set()
        plant_caps = {}
        for number, row in enumerate(rows, 2):
            key = tuple(row[k] for k in keys)
            if key in seen:
                raise ValueError(f"Duplicate {keys} at row {number}")
            seen.add(key)
            try:
                if table == "orders":
                    _number(row["qty"], "qty", integer=True, positive=True)
                    date.fromisoformat(row["due"])
                elif table == "plants":
                    cap = _number(row["weekly_capacity"], "weekly_capacity", integer=True, positive=True)
                    if row["plant"] in plant_caps and plant_caps[row["plant"]] != cap:
                        raise ValueError("Shared capacity differs across products")
                    plant_caps[row["plant"]] = cap
                    for column in ("unit_cost", "energy_kwh_per_unit", "carbon_kg_per_kwh"):
                        _number(row[column], column)
                    for column, value in row.items():
                        if (column == "energy_price_per_kwh" or column.startswith("carbon_kg_per_kwh_week_")) and value:
                            _number(value, column)
                elif table == "transport_duties":
                    _number(row["transit_days"], "transit_days", integer=True)
                    for column in ("transport_cost_per_unit", "carbon_kg_per_unit"):
                        _number(row[column], column)
                    _number(row["duty_pct"], "duty_pct", fraction=True)
                elif table == "capacity_events":
                    first, last = date.fromisoformat(row["from_date"]), date.fromisoformat(row["to_date"])
                    if last < first:
                        raise ValueError("Capacity event ends before it starts")
                    _number(row["capacity_loss_pct"], "capacity_loss_pct", fraction=True)
            except ValueError as e:
                raise ValueError(f"{table}.csv row {number}: {e}") from e
    except csv.Error as e:
        raise ValueError(f"Invalid CSV format: {e}") from e
    return text, len(rows)


def load_snapshot(tables, versions, *, start_date, weeks, energy_price_default):
    missing = REQUIRED_TO_SOLVE - set(tables)
    if missing:
        raise ValueError(f"Upload required tables before analysis: {', '.join(sorted(missing))}")
    with tempfile.TemporaryDirectory(prefix="triplan-input-") as folder:
        for name, content in tables.items():
            (Path(folder) / f"{name}.csv").write_text(content, encoding="utf-8")
        data = load_dataset(folder, start_date=start_date, weeks=weeks,
                            energy_price_default=energy_price_default)
    return replace(data, source="sqlite-snapshot:" + ":".join(
        f"{name}={checksum[:12]}" for name, checksum in sorted(versions.items())))
