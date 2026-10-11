"""Solver-independent model: which options are allowed, objective coefficients, capacity rows.

Decision: x[i] = 1 if option i (order, plant, week) is used; each order is made whole in one plant-week.
Slack u[o] = 1 marks an order that cannot be placed; it carries a penalty far above any real cost,
so the solver always returns and the plan reports the order as infeasible instead of hiding it.

Objective (weights apply to range-normalised terms, so 70/30 means 70% of the cost-carbon trade-off):
  w_cost * cost / cost_range + w_carbon * carbon / carbon_range + lateness + stability
  cost_range   = sum over orders of (dearest - cheapest allowed option)
  carbon_range = sum over orders of (dirtiest - greenest allowed option)
Normalising by the achievable range, not the total, stops the objective whose options differ more in
relative terms (here carbon) from swamping the weights.
"""
from dataclasses import dataclass, field

import pandas as pd

from app.config import cfg

SCALE = 10_000_000          # integer scaling for CP-SAT
UNMET_PENALTY = 100 * SCALE  # one unplaced order outweighs any feasible plan
FLAGGED_LATE_PER_DAY = 0.5   # never_late policy: keep unavoidable lateness as small as capacity allows


@dataclass
class Model:
    options: pd.DataFrame                    # allowed options, with column 'coef' (int)
    orders: list[str]
    caps: dict[tuple, float]                 # capacity key -> units
    cap_mode: str
    flagged: set[str]                        # orders with no on-time option in any plant
    no_option: list[str]                     # orders with no eligible option at all
    carbon_cap_kg: float | None = None
    force_orders: set[str] = field(default_factory=set)  # orders that must be placed (frontier)
    refs: dict = field(default_factory=dict)


def build_model(options: pd.DataFrame, orders: list[str], caps: dict, cap_mode: str, weights: dict,
                policy: str, baseline: dict[str, str] | None = None, carbon_cap_kg: float | None = None,
                force_orders: set[str] | None = None) -> Model:
    o = options.copy()
    has_on_time = o.groupby("order_id")["on_time"].any()
    flagged = set(has_on_time[~has_on_time].index)
    if policy == "never_late":
        o = o[o["on_time"] | o["order_id"].isin(flagged)]
    no_option = sorted(set(orders) - set(options["order_id"]))

    n = max(1, len(orders))
    g = o.groupby("order_id")
    cost_ref = max(1.0, (g["landed_cost"].max() - g["landed_cost"].min()).sum())
    carbon_ref = max(1.0, (g["carbon_kg"].max() - g["carbon_kg"].min()).sum())
    wc, wk = float(weights.get("cost", 0.7)), float(weights.get("carbon", 0.3))
    total = wc + wk or 1.0
    wc, wk = wc / total, wk / total
    late_w = float(weights.get("lateness") or 0) or cfg()["late_penalty_per_day"]
    per_day = [(FLAGGED_LATE_PER_DAY if oid in flagged else late_w) / n for oid in o["order_id"]]
    # measured from each order's best option: same optimum (one option per order), and every term stays
    # within 0..1, so an unplaced order (UNMET_PENALTY) always costs more than any real option
    cost_rel = o["landed_cost"] - g["landed_cost"].transform("min")
    carbon_rel = o["carbon_kg"] - g["carbon_kg"].transform("min")
    score = (wc * cost_rel / cost_ref + wk * carbon_rel / carbon_ref
             + pd.Series(per_day, index=o.index) * o["days_late"])
    if baseline:
        moved = [baseline.get(oid) not in (None, p) for oid, p in zip(o["order_id"], o["plant"], strict=False)]
        score = score + pd.Series(moved, index=o.index).astype(float) * cfg()["stability_penalty"]
    o["coef"] = (score * SCALE).round().astype(int)
    o["cap_key"] = [(p, None if cap_mode == "plant" else pr, w)
                    for p, pr, w in zip(o["plant"], o["product"], o["week"], strict=False)]
    o = o[o["cap_key"].isin(caps.keys())].reset_index(drop=True)
    return Model(o, list(orders), caps, cap_mode, flagged, no_option, carbon_cap_kg,
                 set(force_orders or ()), {"cost_range": round(cost_ref, 2), "carbon_range_kg": round(carbon_ref, 2),
                                           "weights_normalised": {"cost": wc, "carbon": wk}})


def closest_option(options: pd.DataFrame, order_id: str) -> dict | None:
    """Best option for one order ignoring capacity: least late, then cheapest."""
    o = options[options["order_id"] == order_id]
    if o.empty:
        return None
    r = o.sort_values(["days_late", "landed_cost"]).iloc[0]
    return {"plant": r["plant"], "week": int(r["week"]), "landed_cost": float(r["landed_cost"]),
            "carbon_kg": float(r["carbon_kg"]), "arrival": r["arrival"].isoformat(),
            "days_late": int(r["days_late"])}
