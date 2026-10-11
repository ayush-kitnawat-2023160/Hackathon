"""One LangGraph workflow around the independently tested allocation engine."""

import json
import re
import uuid
from datetime import timedelta
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field

from app.allocator import solve_allocation, solve_tradeoff, compare_plans
from app.allocator.candidates import create_candidates
from app.llm import LanguageModel


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["allocate", "replan", "tradeoff", "explain", "brief", "clarification", "refused"]
    objective: Literal["cost", "carbon", "balanced"] = "balanced"
    cost_weight: float = Field(default=0.7, ge=0)
    carbon_weight: float = Field(default=0.3, ge=0)
    plant: str | None = None
    capacity_loss_pct: float | None = Field(default=None, ge=0, le=100)
    carbon_cut_pct: float | None = Field(default=None, ge=0, le=100)
    order_id: str | None = None
    from_date: str | None = None
    to_date: str | None = None
    reason: str | None = None


class State(TypedDict, total=False):
    request: str
    weights: dict
    scenario: dict
    data: object
    latest: dict | None
    versions: dict
    intent: dict
    config: dict
    plan: dict
    baseline: dict
    comparison: dict
    evidence: dict
    summary: str
    answer_type: str
    steps: list
    llm_calls: int
    notes: list


def _parse_rules(request):
    text = request.lower()
    if any(s in text for s in ("ignore capacity", "bypass capacity", "ignore eligibility",
                                "disable validation", "invent numbers", "fake numbers")):
        return Intent(kind="refused", reason="Cannot bypass business constraints or invent KPIs")
    if "add plant" in text or "new plant" in text:
        return Intent(kind="clarification", reason="Upload the updated plants and transport/duty CSVs first")
    if any(s in text for s in ("management brief", "leadership brief", "write the brief", "executive brief")):
        return Intent(kind="brief")
    if any(s in text for s in ("why was", "why is", "explain order", "why did")):
        match = re.search(r"(?:order\s+)([a-z0-9_-]+)", request, re.I)
        return Intent(kind="explain", order_id=match.group(1).upper() if match else None)
    cut = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:carbon|emissions?)\s*(?:cut|reduction|lower|less)", text)
    if not cut:
        cut = re.search(r"(?:reduce|cut|lower)\s*(?:the\s*)?(?:carbon|emissions?)\s*(?:by\s*)?(\d+(?:\.\d+)?)\s*%", text)
    if cut:
        return Intent(kind="tradeoff", carbon_cut_pct=float(cut.group(1)))
    if re.search(r"(?:what does|how much).*carbon.*cost", text) and not cut:
        return Intent(kind="clarification", reason="Specify the requested carbon reduction percentage")
    if any(s in text for s in ("loses", "lost capacity", "capacity loss", "re-plan", "replan", "outage", "disruption")):
        p = re.search(r"\bplant\s+([a-z0-9_-]+)", request, re.I)
        pct = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
        if p and pct:
            return Intent(kind="replan", plant=p.group(1).upper(), capacity_loss_pct=float(pct.group(1)))
        if "event" in text or "uploaded" in text or "outage" in text:
            return Intent(kind="replan")
        return Intent(kind="clarification", reason="Specify the affected plant and capacity-loss percentage")
    if "cheapest" in text or "minimize cost" in text or "minimum cost" in text:
        return Intent(kind="allocate", objective="cost")
    if "greenest" in text or "minimum carbon" in text or "minimize carbon" in text:
        return Intent(kind="allocate", objective="carbon")
    cost = re.search(r"(\d+(?:\.\d+)?)\s*%\s*cost", text)
    carbon = re.search(r"(\d+(?:\.\d+)?)\s*%\s*carbon", text)
    if cost and carbon:
        return Intent(kind="allocate", objective="balanced", cost_weight=float(cost.group(1)),
                      carbon_weight=float(carbon.group(1)))
    if "cut carbon" in text or "reduce carbon" in text:
        return Intent(kind="clarification", reason="Specify a carbon reduction target or objective")
    if any(s in text for s in ("allocate", "allocation", "optimize", "optimise", "plan", "orders")):
        return Intent(kind="allocate")
    return Intent(kind="clarification", reason="Ask to allocate orders, compare plans, re-plan or explain a saved order")


ROUTER_SYSTEM = """You extract parameters for an optimization solver, never allocate orders yourself.
Return ONLY a JSON object with fields: kind (allocate,replan,tradeoff,explain,brief,clarification,refused),
objective (cost,carbon,balanced), cost_weight, carbon_weight, plant, capacity_loss_pct,
carbon_cut_pct, order_id, from_date, to_date, reason. Omit unused fields.
Only use facts explicitly present in the user's request. Never agree to bypass capacity or eligibility.
Ask for clarification if a required plant, percentage or order identifier is missing.
All percentages are 0..100. Default objective balanced, weights cost .7, carbon .3."""


def create_workflow(llm=None):
    model = llm or LanguageModel()

    def log(state, agent, tool=None, output=None):
        return [*state.get("steps", []), {"agent": agent, "tool": tool, "output": output or {}}]

    def router(state):
        request = state["request"]
        calls = state.get("llm_calls", 0)
        notes = list(state.get("notes", []))
        intent = None
        if not model.mock:
            try:
                calls += 1
                intent = Intent.model_validate(model.json(ROUTER_SYSTEM, request))
            except Exception as e:
                notes.append(f"LLM parser unavailable or invalid; deterministic parser used ({type(e).__name__})")
        if intent is None:
            intent = _parse_rules(request)
        weights = state.get("weights") or {}
        if weights and intent.objective == "balanced":
            cost = weights.get("cost", intent.cost_weight)
            carbon = weights.get("carbon", intent.carbon_weight)
            intent = intent.model_copy(update={"cost_weight": float(cost), "carbon_weight": float(carbon),
                "objective": "balanced"})
        return {"intent": intent.model_dump(), "llm_calls": calls, "notes": notes,
                "steps": log(state, "router", "parse_intent", intent.model_dump())}

    def guard(state):
        intent = Intent.model_validate(state["intent"])
        reject = intent.kind in ("refused", "clarification")
        reason = intent.reason
        if any(s in state["request"].lower() for s in
               ("ignore capacity", "bypass capacity", "ignore eligibility",
                "disable validation", "invent numbers", "fake numbers")):
            return {"answer_type": "refused",
                    "summary": "Cannot bypass plant capacity, eligibility or numerical evidence rules",
                    "steps": log(state, "guard", "validate_intent", {"status": "refused"})}
        if not reject and intent.cost_weight + intent.carbon_weight <= 0:
            reason, reject = "Cost and carbon weights cannot both be zero", True
        if not reject and intent.kind == "replan" and intent.plant and intent.plant not in state["data"].capacities:
            reason, reject = f"Unknown plant {intent.plant}", True
        if not reject and intent.kind == "explain" and not intent.order_id:
            reason, reject = "Specify the order ID to explain", True
        if not reject and intent.kind == "tradeoff" and intent.carbon_cut_pct is None:
            reason, reject = "Specify the carbon reduction percentage", True
        if reject:
            kind = "refused" if intent.kind == "refused" else "clarification_needed"
            return {"answer_type": kind, "summary": reason or "Request needs clarification",
                    "steps": log(state, "guard", "validate_intent", {"status": kind, "reason": reason})}
        return {"steps": log(state, "guard", "validate_intent", {"status": "accepted"})}

    def route(state):
        if state.get("answer_type"):
            return "writer"
        return "lookup" if state["intent"]["kind"] in ("explain", "brief") else "solver"

    def solver(state):
        intent = Intent.model_validate(state["intent"])
        data = state["data"]
        config = {"objective": intent.objective, "cost_weight": intent.cost_weight,
                  "carbon_weight": intent.carbon_weight, "allow_fallback": True}
        scenario = state.get("scenario") or {}
        if intent.kind == "tradeoff":
            result = solve_tradeoff(data, carbon_cut_pct=intent.carbon_cut_pct)
            plan = result.get("greener") or result.get("baseline") or {}
            baseline = result.get("baseline") or {}
            comparison = result.get("comparison") or {}
            answer_type = "scenario" if plan.get("validator", {}).get("valid") else "infeasible"
            evidence = {"carbon_cut_pct": intent.carbon_cut_pct, "target_carbon_kg": result.get("target_carbon_kg")}
            return {"config": config, "plan": plan, "baseline": baseline, "comparison": comparison,
                    "evidence": evidence, "answer_type": answer_type,
                    "steps": log(state, "scenario", "solve_tradeoff", {"status": result["status"]})}
        if intent.kind == "replan":
            baseline = solve_allocation(data, {**config, "use_capacity_events": False})
            # An inline what-if should not silently combine with an uploaded outage.
            config["use_capacity_events"] = not bool(intent.plant)
            if intent.plant and intent.capacity_loss_pct is not None:
                start = scenario.get("from_date") or intent.from_date or data.start.isoformat()
                end = scenario.get("to_date") or intent.to_date or (data.start + timedelta(days=13)).isoformat()
                config["additional_capacity_events"] = [{"event_id": "WHAT_IF", "plant": intent.plant,
                    "from_date": start, "to_date": end, "capacity_loss_pct": intent.capacity_loss_pct / 100}]
            plan = solve_allocation(data, config)
            comparison = (compare_plans(baseline, plan)
                          if baseline.get("validator", {}).get("valid") and plan.get("validator", {}).get("valid") else {})
            return {"config": config, "plan": plan, "baseline": baseline, "comparison": comparison,
                    "answer_type": "scenario" if plan.get("mode") == "strict" and plan.get("validator", {}).get("valid") else "infeasible",
                    "steps": log(state, "scenario", "solve_allocation", {"status": plan.get("status"),
                        "baseline_status": baseline.get("status"), "changes": comparison.get("orders_moved")})}
        plan = solve_allocation(data, config)
        return {"config": config, "plan": plan,
                "answer_type": "allocation" if plan.get("mode") == "strict" and plan.get("validator", {}).get("valid") else "infeasible",
                "steps": log(state, "optimiser", "solve_allocation", {"status": plan.get("status")})}

    def checker(state):
        plan = state.get("plan", {})
        verified = bool(plan.get("validator", {}).get("valid"))
        if plan and plan.get("allocation") and not verified:
            return {"answer_type": "infeasible", "summary": "Solver result did not pass validation",
                    "steps": log(state, "checker", "validate_allocation", {"valid": False})}
        return {"steps": log(state, "checker", "validate_allocation",
                              {"valid": verified, "status": plan.get("status")})}

    def lookup(state):
        kind = state["intent"]["kind"]
        previous = state.get("latest")
        if not previous:
            return {"summary": "Run an allocation first so there are verified results to explain.",
                    "answer_type": "clarification_needed",
                    "steps": log(state, "explainer", "get_saved_plan", {"found": False})}
        if previous.get("input_versions") != state["versions"]:
            return {"summary": "Input data changed since the saved plan. Re-run allocation before querying it.",
                    "answer_type": "clarification_needed", "steps": log(state, "explainer", "get_saved_plan", {"stale": True})}
        if kind == "brief":
            return {"evidence": {"previous": previous}, "answer_type": "brief",
                    "steps": log(state, "writer", "get_kpis", {"run_id": previous["run_id"]})}
        oid = state["intent"]["order_id"].upper()
        row = next((r for r in previous.get("allocation", []) if r["order_id"].upper() == oid), None)
        if not row:
            return {"summary": f"Order {oid} has no assignment in the last saved plan.",
                    "answer_type": "clarification_needed", "steps": log(state, "explainer", "explain_order", {"found": False})}
        options = [{"plant": c.plant, "week": c.week, "cost": c.cost,
                    "carbon_kg": c.carbon_kg, "arrival": c.arrival.isoformat(),
                    "on_time": c.late_days == 0}
                   for c in create_candidates(state["data"]) if c.order_id.upper() == oid]
        return {"evidence": {"order": row, "options": options, "run_id": previous["run_id"]},
                "answer_type": "allocation", "steps": log(state, "explainer", "explain_order",
                    {"order_id": oid, "candidate_count": len(options)})}

    def writer(state):
        answer_type = state.get("answer_type", "clarification_needed")
        plan = state.get("plan", {})
        evidence = state.get("evidence", {})
        k = plan.get("kpis", {})
        if state.get("summary"):
            summary = state["summary"]
        elif answer_type == "brief":
            past = evidence["previous"]
            pk = past["kpis"]
            utilisation = pk.get("plant_utilisation") or {}
            busiest = max(utilisation, key=utilisation.get) if utilisation else None
            bottleneck = (f"The busiest plant-week is {busiest} at {utilisation[busiest]:.1%} utilisation. "
                          if busiest is not None else "")
            summary = (
                f"**Management brief — {past['run_id']}**\n\n"
                f"**Decision:** {len(past['allocation'])} orders assigned across the available plants. "
                f"Total landed cost is {pk['total_cost']:,.2f} synthetic currency units, "
                f"with {pk['total_carbon_t']:,.3f} tonnes of carbon emissions.\n\n"
                f"**Delivery and capacity:** {pk['late_orders']} orders arrive late and "
                f"{pk.get('unallocated_orders', 0)} remain unallocated. {bottleneck}"
                "Assignments were checked independently against eligibility and weekly capacity rules.\n\n"
                "**Management action:** Review any unmet or delayed demand before approval. "
                "Use the capacity-loss and carbon-cut scenarios to quantify operational alternatives; "
                "do not assume that lower cost reflects savings if demand goes unfulfilled.\n\n"
                "**Assumptions:** Manufacturing is scheduled by whole week and whole order; "
                "the documented electricity price, production completion and import-duty conventions apply. "
                "The allocation is a proposal, not an automatically approved operating decision."
            )
        elif evidence.get("order"):
            row = evidence["order"]
            summary = (f"Order {row['order_id']} is assigned to Plant {row['plant']} in week {row['week']}. "
                       f"Landed cost {row['cost']:,.2f}, carbon {row['carbon_kg']:,.3f} kg, "
                       f"arrival {row['arrival']}; on time: {row['on_time']}. "
                       "The solver considers all orders, eligibility, weekly capacity and delivery together; "
                       "individual candidate costs alone do not establish the globally optimal decision.")
        elif plan.get("validator", {}).get("valid"):
            summary = (f"{plan['status']} plan: {len(plan['allocation'])} orders allocated, "
                       f"cost {k['total_cost']:,.2f}, carbon {k['total_carbon_t']:,.3f} t, "
                       f"{k['late_orders']} late and {k['unallocated_orders']} unallocated orders.")
            cmp = state.get("comparison", {})
            if cmp:
                summary += (f" Relative to baseline: cost change {cmp['cost_delta']:+,.2f}, "
                            f"carbon change {cmp['carbon_delta_kg']:+,.3f} kg, "
                            f"{cmp['orders_moved']} assignments changed.")
                if cmp.get("comparison_warning"):
                    summary += " " + cmp["comparison_warning"]
        else:
            summary = "No fully feasible allocation was confirmed. " + str(plan.get("reason", plan.get("status", "")))
        notes = list(state.get("notes", []))
        calls = state.get("llm_calls", 0)
        if answer_type not in ("refused", "clarification_needed") and not model.mock:
            try:
                calls += 1
                narrative_evidence = {"answer_type": answer_type,
                    "kpis": k if k else (evidence.get("previous") or {}).get("kpis", {}),
                    "comparison": state.get("comparison", {}),
                    "order": evidence.get("order")}
                narrative = model.complete("""Explain the verified optimization evidence in ONE qualitative sentence.
Use NO numbers, digits, percentages, unsupported facts or recommendations.
Do not invent order assignments. The verified numerical summary is added separately.""",
                    json.dumps(narrative_evidence, default=str))
                if re.search(r"\d", narrative):
                    notes.append("LLM narrative discarded by number guard; used verified summary only")
                elif narrative.strip():
                    summary += " " + narrative.strip()[:400]
            except Exception as e:
                notes.append(f"LLM writer unavailable; deterministic summary used ({type(e).__name__})")
        return {"summary": summary, "llm_calls": calls, "notes": notes,
                "steps": log(state, "writer", "verified_summary", {"number_guard": "template_or_no_digits"})}

    graph = StateGraph(State)
    for name, fn in (("router", router), ("guard", guard), ("solver", solver),
                     ("checker", checker), ("lookup", lookup), ("writer", writer)):
        graph.add_node(name, fn)
    graph.add_edge(START, "router")
    graph.add_edge("router", "guard")
    graph.add_conditional_edges("guard", route, {"solver": "solver", "lookup": "lookup", "writer": "writer"})
    graph.add_edge("solver", "checker")
    graph.add_edge("checker", "writer")
    graph.add_edge("lookup", "writer")
    graph.add_edge("writer", END)
    return graph.compile()


def run_workflow(graph, *, request, weights, scenario, data, versions, latest):
    state = graph.invoke({"request": request, "weights": weights or {}, "scenario": scenario or {},
                          "data": data, "versions": versions, "latest": latest,
                          "steps": [], "llm_calls": 0, "notes": []})
    plan = state.get("plan", {})
    baseline = state.get("baseline", {})
    frontier = []
    baseline_k = baseline.get("kpis", {})
    plan_k = plan.get("kpis", {})
    if baseline.get("validator", {}).get("valid"):
        frontier.append({"label": "Baseline", "carbon_cut_pct": 0,
                         "cost": baseline_k["total_cost"],
                         "carbon_t": baseline_k["total_carbon_t"],
                         "late_orders": baseline_k["late_orders"],
                         "unallocated_orders": baseline_k["unallocated_orders"]})
    if plan.get("validator", {}).get("valid"):
        prior_carbon = baseline_k.get("total_carbon_kg", 0)
        actual_cut = 100 * (prior_carbon - plan_k["total_carbon_kg"]) / prior_carbon if prior_carbon else 0
        frontier.append({"label": "Selected plan", "carbon_cut_pct": round(actual_cut, 3),
                         "cost": plan_k["total_cost"], "carbon_t": plan_k["total_carbon_t"],
                         "late_orders": plan_k["late_orders"],
                         "unallocated_orders": plan_k["unallocated_orders"]})
    recommendation_source = plan if plan_k else state.get("evidence", {}).get("previous", {})
    source_k = recommendation_source.get("kpis", {})
    recommendations = []
    if source_k.get("late_orders", 0) or source_k.get("unallocated_orders", 0):
        recommendations.append({
            "action": "Review affected orders and approve a capacity or delivery recovery scenario",
            "rationale": "The calculated plan leaves some customer demand late or unallocated",
            "evidence_refs": ["kpis.late_orders", "kpis.unallocated_orders"],
            "confidence": "high", "estimated_impact": None, "requires_approval": True,
        })
    result = {"run_id": "tr-" + uuid.uuid4().hex[:12], "answer_type": state["answer_type"],
              "summary": state["summary"], "allocation": plan.get("allocation", []),
              "kpis": plan_k, "tradeoff": frontier,
              "weights_used": {"cost": state.get("config", {}).get("cost_weight", 0.7),
                               "carbon": state.get("config", {}).get("carbon_weight", 0.3)},
              "validator": plan.get("validator", {}), "recommendations": recommendations,
              "assumptions": plan.get("assumptions", []) + [
                  "Delivery is hard on time by default; infeasible cases are labeled best effort."],
              "limitations": plan.get("limitations", []) + state.get("notes", []),
              "solver_status": plan.get("solver_status"), "input_versions": versions,
              "llm_calls": state.get("llm_calls", 0), "tokens": None}
    if state.get("baseline"):
        result["baseline_kpis"] = state["baseline"].get("kpis", {})
    if state.get("comparison"):
        result["comparison"] = state["comparison"]
    if state.get("evidence"):
        result["evidence"] = state["evidence"]
    if state.get("config"):
        result["parameters_used"] = state["config"]
    return result, state["steps"]
