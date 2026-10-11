"""One LangGraph graph. Only `router` and `writer` call the LLM; every other node runs plain, tested code.

router -> guard -> {optimiser | scenario | explainer | brief_data} -> checker -> writer -> number_guard -> assemble
guard can short-circuit to assemble with answer_type refused or clarification_needed.
"""
import time
from datetime import date
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from app.agent import llm, templates
from app.agent.guard import check_numbers
from app.agent.intent import Intent
from app.tools.explain import explain_order
from app.tools.kpis import compute_kpis
from app.tools.planner import LIMITATIONS, assumptions, events_from_table, make_ctx, run_plan
from app.tools.scenario import compare_plans
from app.tools.tradeoff import tradeoff_frontier
from app.trace.store import latest_run


class State(TypedDict, total=False):
    request: str
    weights: dict
    policy: str
    start: date | None
    scenario_json: dict | None
    intent: Intent
    steps: list[dict]
    llm_calls: int
    tokens: int
    answer_type: str
    ctx: Any
    result: dict           # baseline or main plan: {plan, validator, kpis}
    scen: dict | None      # scenario plan
    evidence: dict
    text: str
    retry: int
    bad: list
    gate: dict
    parent_run_id: str | None
    tradeoff: list
    explanation: dict
    comparison: dict
    brief_evidence: dict
    number_check: dict
    text_source: str
    rejected_by_validator: bool


def _step(s: State, agent: str, tool: str | None, inp=None, out=None, t0: float | None = None) -> None:
    s.setdefault("steps", []).append({"agent": agent, "tool": tool, "input": inp, "output": out,
                                      "ms": round((time.perf_counter() - t0) * 1000) if t0 else None})


# ---------- router and guard ----------
def router(s: State) -> State:
    t0 = time.perf_counter()
    sc = s.get("scenario_json") or {}
    if sc.get("type") in ("capacity_loss", "replan"):
        intent = Intent(type="replan", plant=sc.get("plant"), capacity_loss_pct=sc.get("capacity_loss_pct"),
                        from_date=sc.get("from_date"), to_date=sc.get("to_date"))
        meta = {"source": "scenario_json", "llm_calls": 0, "tokens": 0}
    elif sc.get("type") in ("carbon_cut", "tradeoff"):
        cut = sc.get("carbon_cut_pct")
        intent = Intent(type="tradeoff", carbon_cut_pct=cut, all_cuts=cut is None)
        meta = {"source": "scenario_json", "llm_calls": 0, "tokens": 0}
    else:
        intent, meta = llm.parse_intent(s["request"])
    s["intent"] = intent
    s["llm_calls"] = s.get("llm_calls", 0) + meta["llm_calls"]
    s["tokens"] = s.get("tokens", 0) + meta["tokens"]
    _step(s, "router", "parse_intent" if meta["source"] != "scenario_json" else None,
          {"request": s["request"]}, {**intent.model_dump(exclude_none=True, mode="json"), "source": meta["source"]}, t0)
    return s


REFUSALS = {
    "capacity": ("That plan would exceed a plant's weekly capacity, which the allocation rules forbid. "
                 "I can show the cheapest plan that keeps every plant within capacity instead."),
    "eligibility": ("An order can only go to a plant that can make its product. "
                    "I can show the cheapest plan that respects eligibility instead."),
    "delivery": ("Hiding late orders is not allowed: every order that misses its due date is flagged. "
                 "I can show the cheapest plan with late orders listed."),
    "invent_numbers": ("I can only report figures computed by the solver and calculator. "
                       "I can write the brief from the computed results instead."),
}


def guard(s: State) -> State:
    i: Intent = s["intent"]
    g = {"decision": "continue"}
    if len(s["request"]) > 2000:
        g = {"decision": "refused", "message": "The request is too long."}
    elif i.rule_break:
        g = {"decision": "refused", "message": REFUSALS[i.rule_break], "alternative": "Show cheapest valid plan"}
    elif i.type == "unknown":
        g = {"decision": "clarification_needed", "question": "Do you want a plan, a what-if, an explanation or a brief?",
             "options": ["Allocate this week's orders", "What does a 10% carbon cut cost?",
                         "Write the brief for the leadership team"]}
    elif i.type == "tradeoff" and i.carbon_cut_pct is None and not i.all_cuts:
        g = {"decision": "clarification_needed",
             "question": "By how much should carbon fall compared with the current plan?",
             "options": ["5%", "10%", "15%", "20%", "Show all as a trade-off"]}
    elif i.type == "replan" and not i.plant:
        g = {"decision": "clarification_needed", "question": "Which plant loses capacity, and by how much?",
             "options": ["Plant A", "Plant B", "Plant C", "Use capacity_events.csv"]}
    elif i.type == "explain" and not i.order_id:
        g = {"decision": "clarification_needed", "question": "Which order should I explain?", "options": []}
    elif i.carbon_cut_pct is not None and not 0 < i.carbon_cut_pct < 100:
        g = {"decision": "clarification_needed", "question": "Carbon cut must be between 0 and 100 percent.",
             "options": ["5%", "10%", "20%"]}
    s["gate"] = g
    _step(s, "guard", None, None, {"decision": g["decision"]})
    if g["decision"] != "continue":
        s["answer_type"] = g["decision"]
    return s


def route_after_guard(s: State) -> str:
    if s["gate"]["decision"] != "continue":
        return "assemble"
    return {"allocate": "optimiser", "cheapest_on_time": "optimiser", "replan": "scenario",
            "tradeoff": "scenario", "explain": "explainer", "brief": "brief_data"}[s["intent"].type]


# ---------- deterministic agents ----------
def _baseline(s: State) -> dict:
    """The current plan: latest stored allocation with a plan, else solve one now."""
    found = latest_run(("allocation", "infeasible"))
    if found and found[1]["plan"]:
        run_id, r = found
        s["parent_run_id"] = run_id
        plan = r["plan"]
        if s["intent"].type == "replan":  # compare like with like: re-solve with the baseline's own weights
            s["weights"] = r["response"].get("weights_used") or s["weights"]
        _step(s, "optimiser", "load_baseline", {"run_id": run_id},
              {"orders": len(plan["allocation"]), "weights": s["weights"]})
        return {"plan": plan, "validator": r["response"].get("validator"), "kpis": r["response"].get("kpis")}
    t0 = time.perf_counter()
    res = run_plan(s["ctx"], s["weights"], s["policy"])
    _step(s, "optimiser", "solve_allocation", {"weights": s["weights"], "policy": s["policy"]},
          {"status": res["plan"]["status"]}, t0)
    return res


def optimiser(s: State) -> State:
    t0 = time.perf_counter()
    s["ctx"] = make_ctx(s.get("start"))
    _step(s, "optimiser", "build_options", {"start": s["ctx"].start, "weeks": s["ctx"].weeks},
          {"options": len(s["ctx"].options)}, t0)
    if s["intent"].type == "cheapest_on_time":
        s["weights"], s["policy"] = {"cost": 1.0, "carbon": 0.0}, "never_late"
    t0 = time.perf_counter()
    s["result"] = run_plan(s["ctx"], s["weights"], s["policy"])
    p = s["result"]["plan"]
    _step(s, "optimiser", "solve_allocation", {"weights": s["weights"], "policy": s["policy"]},
          {"status": p["status"], "placed": len(p["allocation"]), "unmet": len(p["unmet"]), "solve_ms": p["solve_ms"]}, t0)
    return s


def scenario(s: State) -> State:
    i: Intent = s["intent"]
    if i.type == "tradeoff":
        s["ctx"] = make_ctx(s.get("start"))
        t0 = time.perf_counter()
        s["result"] = run_plan(s["ctx"], s["weights"], s["policy"])
        _step(s, "scenario", "solve_allocation", {"weights": s["weights"]}, {"status": s["result"]["plan"]["status"]}, t0)
        cuts = None if i.all_cuts else [0, i.carbon_cut_pct]
        t0 = time.perf_counter()
        s["tradeoff"] = tradeoff_frontier(s["ctx"], s["weights"], s["policy"], s["result"], cuts)
        _step(s, "scenario", "tradeoff_frontier", {"cuts": cuts or "all"}, s["tradeoff"], t0)
        s["answer_type"] = "scenario"
        return s

    base_ctx = make_ctx(s.get("start"))
    s["ctx"] = base_ctx
    s["result"] = _baseline(s)
    if s["result"]["kpis"] is None:
        s["result"]["kpis"] = compute_kpis(s["result"]["plan"], base_ctx.caps)
    if i.capacity_loss_pct is None:
        events = events_from_table(base_ctx.tables, i.plant)
    else:
        table_ev = events_from_table(base_ctx.tables, i.plant)
        f = i.from_date or (table_ev[0]["from_date"] if table_ev else base_ctx.start)
        to = i.to_date or (table_ev[0]["to_date"] if table_ev else date.fromordinal(base_ctx.start.toordinal() + 7 * base_ctx.weeks - 1))
        events = [{"event_id": "manual", "plant": i.plant, "from_date": f, "to_date": to,
                   "capacity_loss_pct": i.capacity_loss_pct}]
    if not events:
        s["gate"] = {"decision": "clarification_needed",
                      "question": f"How much capacity does Plant {i.plant} lose, and for which dates?",
                      "options": ["10%", "30%", "50%"]}
        s["answer_type"] = "clarification_needed"
        return s
    t0 = time.perf_counter()
    s["ctx"] = make_ctx(s.get("start"), events)
    _step(s, "scenario", "apply_scenario", {"events": events},
          {"weeks_affected": [(a["plant"], a["week"], a["capacity"]) for a in s["ctx"].applied]}, t0)
    baseline_map = {a["order_id"]: a["plant"] for a in s["result"]["plan"]["allocation"]}
    t0 = time.perf_counter()
    s["scen"] = run_plan(s["ctx"], s["weights"], s["policy"], baseline=baseline_map)
    _step(s, "scenario", "solve_allocation", {"weights": s["weights"], "stability_penalty": True},
          {"status": s["scen"]["plan"]["status"]}, t0)
    s["answer_type"] = "scenario"
    return s


def explainer(s: State) -> State:
    s["ctx"] = make_ctx(s.get("start"))
    s["result"] = _baseline(s)
    t0 = time.perf_counter()
    s["explanation"] = explain_order(s["ctx"], s["result"]["plan"], s["intent"].order_id)
    _step(s, "explainer", "explain_order", {"order_id": s["intent"].order_id},
          {k: v for k, v in s["explanation"].items() if k != "options"}, t0)
    s["answer_type"] = "allocation"
    return s


def brief_data(s: State) -> State:
    t0 = time.perf_counter()
    s["ctx"] = make_ctx(s.get("start"))
    s["result"] = _baseline(s)
    if not s["result"].get("kpis"):
        s["result"]["kpis"] = compute_kpis(s["result"]["plan"], s["ctx"].caps)
    ev = {"kpis": s["result"]["kpis"], "sources": {"kpis": s.get("parent_run_id") or "this run"}}
    scen = latest_run(("scenario",), with_plan=False)
    if scen:
        resp = scen[1]["response"]
        if resp.get("comparison"):
            ev["comparison"] = {k: v for k, v in resp["comparison"].items() if k != "moved"}
            ev["sources"]["comparison"] = scen[0]
        if resp.get("tradeoff"):
            ev["tradeoff"] = resp["tradeoff"]
            ev["sources"]["tradeoff"] = scen[0]
    s["brief_evidence"] = ev
    _step(s, "writer", "get_kpis", None, {"sources": ev["sources"]}, t0)
    s["answer_type"] = "brief"
    return s


def checker(s: State) -> State:
    for key in ("result", "scen"):
        r = s.get(key)
        if not r:
            continue
        t0 = time.perf_counter()
        if r.get("validator") is None:
            _step(s, "checker", "validate_plan", None, {"status": r["plan"]["status"], "plan": "none"}, t0)
            s["answer_type"] = "infeasible"
            continue
        v = r["validator"]
        _step(s, "checker", "validate_plan", None,
              {k: v[k] for k in ("capacity_ok", "eligibility_ok", "delivery_ok", "valid")}, t0)
        _step(s, "checker", "compute_kpis", None, r["kpis"])
        if not v["valid"]:
            s["answer_type"] = "infeasible"
            s["rejected_by_validator"] = True
    if s.get("scen") and s["scen"].get("kpis"):
        t0 = time.perf_counter()
        eff = {(a["plant"], a["product"], a["week"]): a["capacity"] for a in s["ctx"].applied}
        s["comparison"] = compare_plans(s["result"]["plan"], s["scen"]["plan"], s["result"]["kpis"], s["scen"]["kpis"], eff)
        _step(s, "scenario", "compare_plans", None,
              {k: s["comparison"][k] for k in ("extra_cost", "extra_carbon_t", "orders_moved", "delta_late_orders")}, t0)
    if s.get("answer_type") in (None, "allocation") and s.get("result") and s["intent"].type != "explain":
        s["answer_type"] = "infeasible" if s["result"]["plan"]["unmet"] else "allocation"
    return s


# ---------- writer and number guard ----------
def _evidence(s: State) -> tuple[str, dict, str]:
    at, i = s["answer_type"], s["intent"]
    if at == "brief":
        e = s["brief_evidence"]
        return "Write a one-page management brief in Markdown with sections: headline, The plan, Cost of cutting carbon (if in evidence), If capacity is lost (if in evidence), Decisions needed.", e, templates.brief_text(e)
    if i.type == "explain":
        e = {"explanation": s["explanation"]}
        return "Explain in 2-4 sentences why this order went to the chosen plant.", e, templates.explain_text(e)
    if i.type == "tradeoff":
        e = {"tradeoff": s["tradeoff"], "kpis": s["result"]["kpis"]}
        return "Summarise the cost of the carbon cuts in 2-3 sentences.", e, templates.tradeoff_text(e)
    if s.get("comparison"):
        e = {"comparison": {k: v for k, v in s["comparison"].items() if k != "moved"},
             "moved_sample": s["comparison"]["moved"][:10]}
        return "Explain in 2-4 sentences what changed in the re-plan and why.", e, templates.replan_text(e)
    r = s["result"]
    e = {"kpis": r["kpis"] or {}, "flagged_late": r["plan"]["flagged_late"],
         "unmet": [{"order_id": u["order_id"], "reason": u["reason"]} for u in r["plan"]["unmet"]][:20]}
    if not r["kpis"]:
        return "", e, "No valid plan could be produced; see the solver status and the unmet orders."
    return "Summarise this allocation in 2-3 sentences.", e, templates.allocation_text(e)


def writer(s: State) -> State:
    t0 = time.perf_counter()
    task, ev, fallback = _evidence(s)
    text, meta = (llm.narrate(task, ev, s.get("bad")) if task else (None, {"llm_calls": 0, "tokens": 0}))
    s["llm_calls"] = s.get("llm_calls", 0) + meta["llm_calls"]
    s["tokens"] = s.get("tokens", 0) + meta["tokens"]
    s["evidence"] = ev
    s["text"] = text if text else fallback
    s["text_source"] = "llm" if text else "template"
    _step(s, "writer", "narrate", {"retry": s.get("retry", 0)}, {"source": s["text_source"]}, t0)
    return s


def number_guard(s: State) -> State:
    r = check_numbers(s["text"], s["evidence"])
    _step(s, "number_guard", "check_numbers", None, r)
    s["number_check"] = r
    if not r["passed"]:
        s["retry"] = s.get("retry", 0) + 1
        s["bad"] = r["unmatched"]
        if s["retry"] >= 2 or s["text_source"] == "template":
            s["text"] = _evidence(s)[2]
            s["text_source"] = "template"
            s["number_check"] = {**check_numbers(s["text"], s["evidence"]), "fallback": "template"}
            s["retry"] = 99
    return s


def route_after_numbers(s: State) -> str:
    return "writer" if not s["number_check"]["passed"] and s.get("retry", 0) < 2 else "assemble"


def assemble(s: State) -> State:
    return s


def build_graph():
    g = StateGraph(State)
    for name, fn in [("router", router), ("guard", guard), ("optimiser", optimiser), ("scenario", scenario),
                     ("explainer", explainer), ("brief_data", brief_data), ("checker", checker),
                     ("writer", writer), ("number_guard", number_guard), ("assemble", assemble)]:
        g.add_node(name, fn)
    g.set_entry_point("router")
    g.add_edge("router", "guard")
    # explicit path maps: the compiled graph then states exactly the routes in the design review (Figure 2)
    g.add_conditional_edges("guard", route_after_guard,
                            ["optimiser", "scenario", "explainer", "brief_data", "assemble"])
    for n in ("optimiser", "scenario", "explainer"):
        g.add_conditional_edges(n, lambda s: "assemble" if s.get("answer_type") == "clarification_needed" else "checker",
                                ["checker", "assemble"])
    g.add_edge("brief_data", "writer")
    g.add_edge("checker", "writer")
    g.add_edge("writer", "number_guard")
    g.add_conditional_edges("number_guard", route_after_numbers, ["writer", "assemble"])
    g.add_edge("assemble", END)
    return g.compile()


GRAPH = build_graph()


def run_graph(request: str, weights: dict, policy: str, start: date | None, scenario: dict | None) -> State:
    return GRAPH.invoke({"request": request, "weights": weights, "policy": policy, "start": start,
                         "scenario_json": scenario, "steps": [], "llm_calls": 0, "tokens": 0})


def build_assumptions(s: State) -> list[str]:
    ctx = s.get("scen") and s.get("ctx") or s.get("ctx")
    out = assumptions(ctx, s["weights"], s["policy"]) if ctx else []
    if s.get("intent"):
        out.append("Parsed request: " + ", ".join(f"{k}={v}" for k, v in
                                                  s["intent"].model_dump(exclude_none=True, mode="json").items()
                                                  if v not in (False, None)))
    return out


__all__ = ["run_graph", "build_assumptions", "LIMITATIONS"]
