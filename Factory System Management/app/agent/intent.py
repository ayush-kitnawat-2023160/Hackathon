"""Typed Intent (the Router's output) and the rule-based parser used for MOCK_LLM and as fallback."""
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

IntentType = Literal["allocate", "cheapest_on_time", "tradeoff", "replan", "explain", "brief", "unknown"]


class Intent(BaseModel):
    type: IntentType = Field(description="allocate: assign orders to plants; cheapest_on_time: cheapest plan "
                             "with no late orders; tradeoff: cost of cutting carbon; replan: a plant loses "
                             "capacity; explain: why an order went to a plant; brief: management brief")
    plant: str | None = Field(None, description="Plant letter for replan, e.g. 'B'")
    capacity_loss_pct: float | None = Field(None, description="Percent of capacity lost, 0-100")
    from_date: date | None = None
    to_date: date | None = None
    carbon_cut_pct: float | None = Field(None, description="Carbon cut in percent for tradeoff; null if not given")
    all_cuts: bool = Field(False, description="True if the user wants the whole trade-off curve")
    order_id: str | None = Field(None, description="Order id for explain, e.g. 'O-077'")
    weights: dict[str, float] | None = Field(None, description="Only if the user states cost/carbon weights")
    rule_break: Literal["capacity", "eligibility", "invent_numbers", "delivery"] | None = Field(
        None, description="Set if the user asks to ignore capacity, eligibility or delivery rules, or to state "
                          "numbers the tools did not compute")


def normalise_order_id(raw: str) -> str:
    m = re.search(r"(\d+)", raw)
    return f"O-{int(m.group(1)):03d}" if m else raw


def rule_parse(text: str) -> Intent:
    t = text.lower()
    pct = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
    plant = re.search(r"\bplant\s+([a-z])\b", t)
    order = re.search(r"\border\s*(?:id\s*)?(o-?\d+|\d+)", t)
    weights = None
    wm = re.search(r"(\d+)\s*%?\s*cost\D+(\d+)\s*%?\s*carbon", t) or re.search(r"cost\s*(\d+)\s*/\s*carbon\s*(\d+)", t)
    if wm:
        weights = {"cost": int(wm.group(1)) / 100, "carbon": int(wm.group(2)) / 100}

    rule_break = None
    if re.search(r"ignor\w*\s+(its\s+|the\s+)?capacit|exceed\w*\s+capacit|over\s*capacity|no capacity limit", t):
        rule_break = "capacity"
    elif re.search(r"ignor\w*\s+(the\s+)?(eligib|which plant can)|plant\s+\w+\s+can't make|even if it cannot make", t):
        rule_break = "eligibility"
    elif re.search(r"make up|invent|guess (the )?numbers|estimate without|round up the savings|say (it|we) sav", t):
        rule_break = "invent_numbers"

    if re.search(r"\bbrief\b|leadership|management summary|executive summary", t):
        kind = "brief"
    elif re.search(r"\bwhy\b", t) and order:
        kind = "explain"
    elif re.search(r"loses?|lost|outage|down by|capacity (drop|loss|cut)|re-?plan", t):
        kind = "replan"
    elif re.search(r"carbon|greener|emission|co2|trade-?off|frontier", t) and not re.search(r"allocate", t):
        kind = "tradeoff"
    elif re.search(r"cheapest.*on[- ]time|on[- ]time.*cheapest", t):
        kind = "cheapest_on_time"
    elif re.search(r"allocat|assign|plan|put every|put all", t):
        kind = "allocate"
    else:
        kind = "unknown"
    return Intent(
        type=kind, rule_break=rule_break, weights=weights,
        plant=plant.group(1).upper() if plant else None,
        capacity_loss_pct=float(pct.group(1)) if pct and kind == "replan" else None,
        carbon_cut_pct=float(pct.group(1)) if pct and kind == "tradeoff" else None,
        all_cuts=bool(re.search(r"trade-?off|frontier|all cuts|curve", t)),
        order_id=normalise_order_id(order.group(1)) if order else None,
    )
