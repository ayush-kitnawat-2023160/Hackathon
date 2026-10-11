"""Glue for the deterministic path: data -> options -> solve -> validate -> KPIs.

The agent nodes and the API call these functions; none of them touches an LLM.
"""
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from app.config import cfg, planning_start
from app.data.store import read_all
from app.data.validate import plant_capacity_mode
from app.models.options import apply_scenario, build_options, capacity_table, horizon_weeks
from app.solver.milp import solve_allocation
from app.solver.validator import validate_plan
from app.tools.kpis import compute_kpis


class DataNotReady(RuntimeError):
    pass


@dataclass
class Ctx:
    tables: dict[str, pd.DataFrame]
    start: date
    weeks: int
    options: pd.DataFrame
    cap_mode: str
    base_caps: dict
    caps: dict                       # effective capacity after any scenario events
    events: list[dict] = field(default_factory=list)
    applied: list[dict] = field(default_factory=list)


def make_ctx(start: date | None = None, events: list[dict] | None = None,
             tables: dict | None = None) -> Ctx:
    t = tables if tables is not None else read_all()
    missing = [n for n in ("orders", "plants", "transport_duties") if n not in t or t[n] is None or not len(t[n])]
    if missing:
        raise DataNotReady(f"Load and validate {', '.join(m + '.csv' for m in missing)} first")
    start = start or planning_start()
    weeks = horizon_weeks(t["orders"], start)
    mode = plant_capacity_mode(t["plants"])
    base = capacity_table(t["plants"], weeks, mode)
    caps, applied = apply_scenario(base, events or [], start)
    return Ctx(t, start, weeks, build_options(t, start, weeks), mode, base, caps, list(events or []), applied)


def run_plan(ctx: Ctx, weights: dict, policy: str, baseline: dict[str, str] | None = None,
             carbon_cap_kg: float | None = None, force_orders: set[str] | None = None,
             backend: str = "cpsat", time_limit: float | None = None, workers: int | None = None) -> dict:
    plan = solve_allocation(ctx.options, ctx.tables["orders"]["order_id"].tolist(), ctx.caps, ctx.cap_mode,
                            weights, policy, baseline, carbon_cap_kg, force_orders, backend, time_limit, workers)
    if not plan["allocation"] and plan["status"] not in ("OPTIMAL", "FEASIBLE"):
        return {"plan": plan, "validator": None, "kpis": None}
    v = validate_plan(plan, ctx.tables, ctx.start, policy, ctx.events)
    return {"plan": plan, "validator": v, "kpis": compute_kpis(plan, ctx.caps)}


def events_from_table(ctx_tables: dict, plant: str | None = None) -> list[dict]:
    ev = ctx_tables.get("capacity_events")
    if ev is None or not len(ev):
        return []
    rows = ev if plant is None else ev[ev["plant"] == plant]
    return rows[["event_id", "plant", "from_date", "to_date", "capacity_loss_pct"]].to_dict("records")


def assumptions(ctx: Ctx, weights: dict, policy: str) -> list[str]:
    c = cfg()
    out = [f"Weights {round(weights.get('cost', 0) * 100)}% cost, {round(weights.get('carbon', 0) * 100)}% carbon, "
           "applied to cost and carbon normalised by each order's best option",
           f"Delivery policy: {'never late; orders with no on-time option are flagged' if policy == 'never_late' else 'late allowed with a penalty per day'}",
           f"Planning starts {ctx.start.isoformat()}; {ctx.weeks} weekly buckets; an order ships on the last day of its production week",
           f"Capacity is {'per plant' if ctx.cap_mode == 'plant' else 'per plant and product'}, in {c['units']['capacity']}",
           "Import duty is charged on production + energy + transport (CIF-style base)"]
    if "energy_price_per_kwh" not in ctx.tables["plants"] or ctx.tables["plants"]["energy_price_per_kwh"].isna().any():
        out.append(f"Energy price defaults to {c['energy_price_per_kwh_default']} per kWh where plants.csv has none")
    for e in ctx.events:
        out.append(f"Capacity event {e.get('event_id') or 'manual'}: Plant {e['plant']} loses "
                   f"{e['capacity_loss_pct']:g}% from {e['from_date']} to {e['to_date']} (pro rata by day)")
    return out


LIMITATIONS = ["Duty rates and energy prices assumed constant for the period",
               "Orders are not split across plants or weeks",
               "Transit times are fixed per lane; no port or customs delays modelled"]
