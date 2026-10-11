"""Schema, type and value checks per row, plus the guide's six cross-file checks (section 4.5).

Bad rows are rejected with a reason; the loader never crashes on a broken file.
"""
from datetime import date

import pandas as pd

from app.config import cfg, planning_start

# Annex B required columns and their types. Extra columns are allowed.
SCHEMA = {
    "orders": {"order_id": "str", "product": "str", "qty": "int", "destination": "str", "due": "date"},
    "plants": {"plant": "str", "product": "str", "weekly_capacity": "int", "unit_cost": "float",
               "energy_kwh_per_unit": "float", "carbon_kg_per_kwh": "float"},
    "transport_duties": {"plant": "str", "destination": "str", "transport_cost_per_unit": "float",
                         "transit_days": "int", "duty_pct": "float", "carbon_kg_per_unit": "float"},
    "capacity_events": {"event_id": "str", "plant": "str", "from_date": "date", "to_date": "date",
                        "capacity_loss_pct": "float"},
}
# Optional columns this system understands (see README, "Schema extensions")
OPTIONAL = {"plants": {"energy_price_per_kwh": "float", "week_start": "date"}}
KEYS = {"orders": ["order_id"], "plants": ["plant", "product", "week_start"],
        "transport_duties": ["plant", "destination"], "capacity_events": ["event_id"]}
MAX_CARBON_KG_PER_KWH = 2.0  # above this the value is almost certainly g/kWh, not kg/kWh


class FileRejected(ValueError):
    """Required columns are missing, so nothing in the file can be loaded."""


def _parse(value: str, kind: str):
    if kind == "str":
        if value == "":
            raise ValueError("is empty")
        return value
    if kind == "int":
        f = float(value)
        if not f.is_integer():
            raise ValueError("must be a whole number")
        return int(f)
    if kind == "float":
        f = float(value)
        if f != f or f in (float("inf"), float("-inf")):
            raise ValueError("is not a finite number")
        return f
    if kind == "date":
        return date.fromisoformat(value)
    raise AssertionError(kind)


def _value_rules(table: str, r: dict, start: date) -> tuple[str, str] | None:
    """Return (column, reason) for the first business rule this row breaks."""
    if table == "orders":
        if r["qty"] <= 0:
            return "qty", "Quantity must be a positive whole number"
        if r["due"] <= start:
            return "due", "Due date is before the planning start"
    elif table == "plants":
        if r["weekly_capacity"] <= 0:
            return "weekly_capacity", "Capacity must be positive (units per week)"
        if r["unit_cost"] < 0 or r["energy_kwh_per_unit"] < 0:
            return "unit_cost", "Costs and energy use cannot be negative"
        if not 0 <= r["carbon_kg_per_kwh"] <= MAX_CARBON_KG_PER_KWH:
            return "carbon_kg_per_kwh", f"Carbon intensity must be kg CO2e per kWh (0 to {MAX_CARBON_KG_PER_KWH})"
        if r.get("energy_price_per_kwh") is not None and r["energy_price_per_kwh"] < 0:
            return "energy_price_per_kwh", "Energy price cannot be negative"
    elif table == "transport_duties":
        if r["transit_days"] < 0:
            return "transit_days", "Transit days cannot be negative"
        if not 0 <= r["duty_pct"] <= 100:
            return "duty_pct", "Duty must be a percentage between 0 and 100"
        if r["transport_cost_per_unit"] < 0 or r["carbon_kg_per_unit"] < 0:
            return "transport_cost_per_unit", "Transport cost and carbon cannot be negative"
    elif table == "capacity_events":
        if not 0 < r["capacity_loss_pct"] <= 100:
            return "capacity_loss_pct", "Capacity loss must be between 0 and 100 percent"
        if r["to_date"] < r["from_date"]:
            return "to_date", "End date is before the start date"
    return None


def validate_table(table: str, df: pd.DataFrame, file: str | None = None,
                   start: date | None = None) -> tuple[pd.DataFrame, list[dict]]:
    """Return (valid rows with typed columns, rejected rows with reasons)."""
    file = file or f"{table}.csv"
    start = start or planning_start()
    missing = [c for c in SCHEMA[table] if c not in df.columns]
    if missing:
        raise FileRejected(f"{file} is missing the required column {', '.join(missing)}. Nothing was loaded.")
    types = {**SCHEMA[table], **{c: t for c, t in OPTIONAL.get(table, {}).items() if c in df.columns}}

    valid, rejects, seen = [], [], set()
    for i, raw in enumerate(df.to_dict("records")):
        row_no = i + 2  # header is line 1
        rec, err = dict(raw), None
        for col, kind in types.items():
            v = raw.get(col, "")
            if v == "" and col in OPTIONAL.get(table, {}):
                rec[col] = None
                continue
            try:
                rec[col] = _parse(v, kind)
            except ValueError as e:
                err = (col, f"'{v}' {e}" if "must" in str(e) or "empty" in str(e) else f"'{v}' is not a valid {kind}")
                break
        if err is None:
            err = _value_rules(table, rec, start)
        key = tuple(rec.get(k) for k in KEYS[table])
        if err is None and key in seen:
            err = (KEYS[table][0], f"Duplicate key {', '.join(str(k) for k in key if k is not None)}")
        if err:
            rejects.append({"file": file, "row": row_no, "column": err[0], "reason": err[1]})
            continue
        seen.add(key)
        valid.append(rec)
    out = pd.DataFrame(valid, columns=list(df.columns) + [c for c in types if c not in df.columns])
    return out, rejects


def cross_checks(t: dict[str, pd.DataFrame]) -> list[dict]:
    """The six checks from guide section 4.5, over whatever tables are loaded."""
    orders, plants, lanes = t.get("orders"), t.get("plants"), t.get("transport_duties")
    checks = []

    def add(name, ok, detail=""):
        checks.append({"check": name, "passed": bool(ok), "detail": detail})

    if orders is not None and plants is not None and len(orders) and len(plants):
        makeable = set(plants["product"])
        bad = orders.loc[~orders["product"].isin(makeable), "order_id"].tolist()
        add("Every order's product can be made in at least one plant", not bad,
            f"No plant makes the product of {', '.join(bad[:10])}" if bad else "")
    else:
        add("Every order's product can be made in at least one plant", False, "orders or plants not loaded")

    if orders is not None and plants is not None and lanes is not None and len(lanes):
        have = set(zip(lanes["plant"], lanes["destination"], strict=False))
        need = {(p, d) for p in set(plants["plant"]) for d in set(orders["destination"])}
        gap = sorted(need - have)
        add("Every plant and destination pair has a transport row", not gap,
            f"Missing: {', '.join(f'{p}->{d}' for p, d in gap[:10])}" if gap else "")
    else:
        add("Every plant and destination pair has a transport row", False, "a table is not loaded")

    unit = cfg()["units"]
    cap_ok = plants is not None and len(plants) and (plants["weekly_capacity"] > 0).all()
    add(f"Capacity is positive, in {unit['capacity']}", cap_ok)
    due_ok = orders is not None and len(orders) and (orders["due"] > planning_start()).all()
    add("Due dates are after the planning start", due_ok)
    ci_ok = plants is not None and len(plants) and plants["carbon_kg_per_kwh"].between(0, MAX_CARBON_KG_PER_KWH).all()
    add(f"Carbon intensity is in {unit['carbon_intensity']}", ci_ok)

    expected = cfg().get("data_card_row_counts") or {}
    mism = [f"{k}: {len(t[k])} loaded, data card says {v}" for k, v in expected.items()
            if v is not None and k in t and len(t[k]) != v]
    add("Row counts match the data card", not mism,
        "; ".join(mism) if mism else ("" if any(v is not None for v in expected.values())
                                      else "no data-card counts configured"))
    return checks


def plant_capacity_mode(plants: pd.DataFrame) -> str:
    """'plant' if each plant states one weekly capacity for all its products, else 'plant_product'."""
    per_plant = plants.groupby("plant")["weekly_capacity"].nunique()
    return "plant" if (per_plant <= 1).all() else "plant_product"
