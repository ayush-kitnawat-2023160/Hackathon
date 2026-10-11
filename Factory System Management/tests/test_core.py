import pandas as pd
import pytest

from app.agent.guard import check_numbers
from app.agent.intent import rule_parse
from app.data.validate import FileRejected, validate_table
from app.models.options import apply_scenario, build_options, capacity_table
from app.solver.validator import validate_plan
from app.tools.planner import make_ctx, run_plan
from tests.conftest import START, d


def test_formulas_by_hand(tiny):
    o = build_options(tiny, START, 2).set_index(["plant", "week"])
    a = o.loc[("A", 1)]
    # production 100*10=1000, energy 100*2*0.1=20, transport 100*1=100, duty 10% of 1120=112
    assert (a.production_cost, a.energy_cost, a.transport_cost, a.duty_cost) == (1000, 20, 100, 112)
    assert a.landed_cost == 1232
    assert a.carbon_kg == 150            # 100 * (2*0.5 + 0.5)
    assert a.arrival == d(16) and a.days_late == 8   # ships day 6, 10 days transit, due day 8
    c = o.loc[("C", 1)]
    assert c.landed_cost == 1290 and c.carbon_kg == 40 and c.on_time


def test_cheapest_plant_late_so_on_time_plant_chosen(tiny):
    r = run_plan(make_ctx(START, tables=tiny), {"cost": 1, "carbon": 0}, "never_late")
    assert r["plan"]["allocation"][0]["plant"] == "C"
    assert r["validator"]["valid"]


def test_allow_late_policy_can_pick_cheaper_late_plant(tiny):
    tiny["orders"].loc[0, "due"] = d(15)   # A arrives day 16: 1 day late
    r = run_plan(make_ctx(START, tables=tiny), {"cost": 1, "carbon": 0, "lateness": 0.0001}, "allow_late")
    assert r["plan"]["allocation"][0]["plant"] == "A"
    assert r["kpis"]["late_orders"] == 1


def test_weights_change_the_plan(tiny):
    tiny["orders"].loc[0, "due"] = d(30)   # both plants on time
    ctx = make_ctx(START, tables=tiny)
    assert run_plan(ctx, {"cost": 1, "carbon": 0}, "never_late")["plan"]["allocation"][0]["plant"] == "A"
    assert run_plan(ctx, {"cost": 0, "carbon": 1}, "never_late")["plan"]["allocation"][0]["plant"] == "C"


def test_capacity_respected_and_infeasible_reported(tiny):
    tiny["plants"].loc[1, "weekly_capacity"] = 150
    tiny["orders"] = pd.DataFrame([
        {"order_id": "O-1", "product": "P1", "qty": 100, "destination": "DE", "due": d(8)},
        {"order_id": "O-2", "product": "P1", "qty": 100, "destination": "DE", "due": d(8)},
    ])
    r = run_plan(make_ctx(START, tables=tiny), {"cost": 0.7, "carbon": 0.3}, "never_late")
    assert len(r["plan"]["allocation"]) == 1
    assert len(r["plan"]["unmet"]) == 1
    assert r["plan"]["unmet"][0]["closest_option"]["plant"] == "C"
    assert r["validator"]["capacity_ok"] and r["validator"]["all_orders_accounted"]


def test_capacity_event_only_hits_its_weeks(tiny):
    cap = capacity_table(tiny["plants"], 3, "plant")
    eff, applied = apply_scenario(cap, [{"plant": "C", "from_date": d(7), "to_date": d(13),
                                         "capacity_loss_pct": 30}], START)
    assert eff[("C", None, 2)] == 700
    assert eff[("C", None, 1)] == eff[("C", None, 3)] == 1000
    assert eff[("A", None, 2)] == 1000
    assert len(applied) == 1


def test_validator_catches_bad_plans(tiny):
    plan = {"allocation": [{"order_id": "O-1", "plant": "A", "week": 1, "arrival": d(16).isoformat(),
                            "on_time": False}], "unmet": []}
    v = validate_plan(plan, tiny, START, "never_late")
    assert not v["delivery_ok"]          # late although Plant C was on time
    plan["allocation"][0]["plant"] = "B"
    assert not validate_plan(plan, tiny, START)["eligibility_ok"]


def test_data_validation_rejects_bad_rows():
    df = pd.DataFrame([
        {"order_id": "O-1", "product": "P1", "qty": "100", "destination": "DE", "due": "2026-10-20"},
        {"order_id": "O-2", "product": "P1", "qty": "-5", "destination": "DE", "due": "2026-10-20"},
        {"order_id": "O-3", "product": "P1", "qty": "10", "destination": "DE", "due": "2026-10-01"},
        {"order_id": "O-1", "product": "P1", "qty": "10", "destination": "DE", "due": "2026-10-20"},
        {"order_id": "O-4", "product": "P1", "qty": "ten", "destination": "DE", "due": "2026-10-20"},
    ])
    valid, rejects = validate_table("orders", df, start=START)
    assert list(valid["order_id"]) == ["O-1"]
    assert [r["row"] for r in rejects] == [3, 4, 5, 6]
    with pytest.raises(FileRejected):
        validate_table("orders", df.drop(columns=["due"]), start=START)


def test_number_guard():
    ev = {"kpis": {"total_cost": 1843210, "total_carbon_t": 410.2, "late_orders": 2}}
    assert check_numbers("Cost 1.84M, carbon 410 t, 2 late orders.", ev)["passed"]
    r = check_numbers("Cost 1.84M and we save 95,000 next quarter.", ev)
    assert not r["passed"] and "95,000" in r["unmatched"]


@pytest.mark.parametrize("text,kind,extra", [
    ("Allocate this week's orders.", "allocate", {}),
    ("What does a 10% carbon cut cost?", "tradeoff", {"carbon_cut_pct": 10}),
    ("What is the cheapest plan that is on time?", "cheapest_on_time", {}),
    ("Plant B loses 30% capacity. Re-plan.", "replan", {"plant": "B", "capacity_loss_pct": 30}),
    ("Why was order 77 built in Plant C?", "explain", {"order_id": "O-077"}),
    ("Write the brief for the leadership team.", "brief", {}),
])
def test_rule_parser(text, kind, extra):
    i = rule_parse(text)
    assert i.type == kind
    for k, v in extra.items():
        assert getattr(i, k) == v


def test_rule_breaks_detected():
    assert rule_parse("Put all orders in Plant A, ignore capacity.").rule_break == "capacity"
    assert rule_parse("Cut carbon.").carbon_cut_pct is None
