"""Calculator: landed cost, carbon and delivery for every eligible order x plant x production week.

Formulas (README, "Cost and carbon model"):
  production = qty * unit_cost
  energy     = qty * energy_kwh_per_unit * energy_price_per_kwh
  transport  = qty * transport_cost_per_unit
  duty       = duty_pct / 100 * (production + energy + transport)      # CIF-style duty base
  landed     = production + energy + transport + duty
  carbon_kg  = qty * (energy_kwh_per_unit * carbon_kg_per_kwh[plant, week] + transport carbon_kg_per_unit)
Time: week w covers start + 7(w-1) .. start + 7w - 1; an order made in week w ships on the week's
last day and arrives transit_days later.
"""
import math
from datetime import date, timedelta

import pandas as pd

from app.config import cfg

OPTION_COLS = ["order_id", "plant", "week", "product", "qty", "destination", "due", "production_cost",
               "energy_cost", "transport_cost", "duty_cost", "landed_cost", "carbon_kg", "ship_date",
               "arrival", "days_late", "on_time"]


def horizon_weeks(orders: pd.DataFrame, start: date) -> int:
    if orders is None or not len(orders):
        return 1
    return max(1, math.ceil((max(orders["due"]) - start).days / 7))


def week_of(d: date, start: date) -> int:
    return (d - start).days // 7 + 1


def ship_date(week: int, start: date) -> date:
    return start + timedelta(days=7 * week - 1)


def plant_rows(plants: pd.DataFrame, start: date, weeks: int) -> pd.DataFrame:
    """One row per plant x product x week, applying week_start overrides for carbon intensity."""
    p = plants.copy()
    if "week_start" not in p:
        p["week_start"] = None
    if "energy_price_per_kwh" not in p:
        p["energy_price_per_kwh"] = None
    p["energy_price_per_kwh"] = p["energy_price_per_kwh"].fillna(cfg()["energy_price_per_kwh_default"])
    base = p[p["week_start"].isna()]
    over = p[p["week_start"].notna()].copy()
    over["week"] = over["week_start"].map(lambda d: week_of(d, start))
    rows = []
    for w in range(1, weeks + 1):
        wk = over[over["week"] == w].drop(columns=["week"])
        overridden = set(zip(wk["plant"], wk["product"], strict=False))
        b = base[[(pl, pr) not in overridden for pl, pr in zip(base["plant"], base["product"], strict=False)]]
        rows.append(pd.concat([b, wk]).assign(week=w))
    return pd.concat(rows, ignore_index=True)


def build_options(t: dict[str, pd.DataFrame], start: date, weeks: int | None = None) -> pd.DataFrame:
    orders, plants, lanes = t["orders"], t["plants"], t["transport_duties"]
    weeks = weeks or horizon_weeks(orders, start)
    pw = plant_rows(plants, start, weeks)
    df = orders.merge(pw, on="product").merge(lanes, on=["plant", "destination"],
                                               suffixes=("_plant", "_lane"))
    if df.empty:
        return pd.DataFrame(columns=OPTION_COLS)
    q = df["qty"]
    df["production_cost"] = q * df["unit_cost"]
    df["energy_cost"] = q * df["energy_kwh_per_unit"] * df["energy_price_per_kwh"]
    df["transport_cost"] = q * df["transport_cost_per_unit"]
    df["duty_cost"] = df["duty_pct"] / 100 * (df["production_cost"] + df["energy_cost"] + df["transport_cost"])
    df["landed_cost"] = df["production_cost"] + df["energy_cost"] + df["transport_cost"] + df["duty_cost"]
    df["carbon_kg"] = q * (df["energy_kwh_per_unit"] * df["carbon_kg_per_kwh"] + df["carbon_kg_per_unit"])
    df["ship_date"] = df["week"].map(lambda w: ship_date(w, start))
    df["arrival"] = [s + timedelta(days=int(d)) for s, d in zip(df["ship_date"], df["transit_days"], strict=False)]
    df["days_late"] = [max(0, (a - d).days) for a, d in zip(df["arrival"], df["due"], strict=False)]
    df["on_time"] = df["days_late"] == 0
    for c in ("production_cost", "energy_cost", "transport_cost", "duty_cost", "landed_cost", "carbon_kg"):
        df[c] = df[c].round(2)
    return df[OPTION_COLS].sort_values(["order_id", "plant", "week"]).reset_index(drop=True)


def capacity_table(plants: pd.DataFrame, weeks: int, mode: str) -> dict[tuple, float]:
    """Weekly capacity keyed (plant, None, week) in 'plant' mode or (plant, product, week) otherwise."""
    cap = {}
    for r in plants[plants.get("week_start", pd.Series(None, index=plants.index)).isna()].itertuples():
        for w in range(1, weeks + 1):
            key = (r.plant, None if mode == "plant" else r.product, w)
            cap[key] = float(r.weekly_capacity)
    return cap


def apply_scenario(cap: dict[tuple, float], events: list[dict], start: date) -> tuple[dict, list[dict]]:
    """Reduce capacity only for the affected plant and the days each event covers (pro rata per week)."""
    eff = dict(cap)
    applied = []
    for e in events:
        f, to = e["from_date"], e["to_date"]
        for key in [k for k in eff if k[0] == e["plant"]]:
            w = key[2]
            ws, we = start + timedelta(days=7 * (w - 1)), start + timedelta(days=7 * w - 1)
            overlap = (min(we, to) - max(ws, f)).days + 1
            if overlap > 0:
                eff[key] = round(eff[key] * (1 - e["capacity_loss_pct"] / 100 * min(overlap, 7) / 7), 2)
                applied.append({"plant": key[0], "product": key[1], "week": w, "capacity": eff[key],
                                "original": cap[key], "event_id": e.get("event_id")})
    return eff, applied
