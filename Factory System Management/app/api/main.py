"""FastAPI app: POST /analyze, POST /ingest, GET /health, GET /trace/{run_id} (plus read-only helpers for the UI)."""
import json
import logging
import time
from contextlib import closing

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.agent import llm
from app.agent.graph import LIMITATIONS, build_assumptions, run_graph
from app.api.schemas import AnalyzeRequest, AnalyzeResponse, Recommendation
from app.config import cfg, env
from app.data.loader import TABLES, LoadError
from app.data.store import connect, datasets, get_rejects, ingest_bytes, read_all
from app.data.validate import FileRejected, cross_checks
from app.tools.planner import DataNotReady
from app.trace.store import get_run, get_trace, list_runs, new_run_id, save_trace

log = logging.getLogger("allocator")
app = FastAPI(title="Multi-Plant Order Allocation", version="1.0", docs_url="/docs")


@app.exception_handler(Exception)
async def _unhandled(_, exc: Exception):  # never leak internals
    log.exception("unhandled error")
    return JSONResponse(status_code=500, content={"detail": "Internal error; see the server log."})


@app.get("/health")
def health():
    try:
        with closing(connect()) as con:
            con.execute("SELECT 1")
        loaded = datasets()
        store = {"status": "ready", "tables": {t: loaded.get(t, {}).get("rows_loaded") for t in TABLES}}
    except Exception as e:  # noqa: BLE001
        store = {"status": "error", "error": type(e).__name__}
    return {"api": "ready", "data_store": store, "llm": llm.health()}


@app.post("/ingest")
async def ingest(file: UploadFile = File(...), meta: str | None = Form(None), table: str | None = Form(None),
                 x_admin_token: str | None = Header(None)):
    token = env("ADMIN_TOKEN")
    if token and x_admin_token != token:
        raise HTTPException(401, "Missing or wrong X-Admin-Token")
    if meta:
        try:
            table = json.loads(meta).get("table", table)
        except (json.JSONDecodeError, AttributeError) as e:
            raise HTTPException(422, "meta must be JSON like {\"table\": \"orders\"}") from e
    if table not in TABLES:
        raise HTTPException(422, f"table must be one of {', '.join(TABLES)}")
    content = await file.read(cfg()["limits"]["max_upload_bytes"] + 1)
    try:
        result = ingest_bytes(content, table)
    except (LoadError, FileRejected) as e:
        return JSONResponse(status_code=422, content={"table": table, "rows_loaded": 0, "rows_rejected": None,
                                                      "status": "rejected", "error": str(e)})
    result["checks"] = cross_checks(read_all())
    return result


@app.get("/data/status")
def data_status():
    return {"datasets": datasets(), "rejects": get_rejects(), "checks": cross_checks(read_all())}


@app.get("/trace/{run_id}")
def trace(run_id: str):
    t = get_trace(run_id[:32])
    if not t:
        raise HTTPException(404, "Unknown run_id")
    return t


@app.get("/runs")
def runs(limit: int = 50):
    return list_runs(min(limit, 200))


@app.get("/runs/{run_id}")
def run(run_id: str):
    r = get_run(run_id[:32])
    if not r:
        raise HTTPException(404, "Unknown run_id")
    return r["response"]


def _status(at: str, s: dict) -> str:
    if s.get("rejected_by_validator"):
        return "rejected_by_validator"
    if at == "infeasible":
        return "infeasible"
    r = s.get("scen") or s.get("result")
    if not r or not r.get("kpis"):
        return "no_plan"
    if r["plan"]["unmet"]:
        return "infeasible"
    return "valid_on_time" if r["kpis"]["late_orders"] == 0 else "valid_late_flagged"


def _recommendations(at: str, s: dict) -> list[Recommendation]:
    recs = []
    r = s.get("scen") or s.get("result")
    conf = "high" if r and r["plan"]["status"] == "OPTIMAL" else "medium"
    if at == "scenario" and s.get("comparison"):
        c = s["comparison"]
        recs.append(Recommendation(
            action="Adopt the scenario plan for the affected weeks",
            rationale=f"{c['orders_moved']} orders move plant; the plan passes every validator check",
            evidence_refs=["compare_plans", "validate_plan"], confidence=conf,
            estimated_impact=f"{c['extra_cost']:+,} cost, {c['extra_carbon_t']:+g} t carbon", requires_approval=True))
    if at == "scenario" and s.get("tradeoff"):
        for p in s["tradeoff"]:
            if p["carbon_cut_pct"] > 0 and p["feasible"]:
                recs.append(Recommendation(
                    action=f"Consider the {p['carbon_cut_pct']:g}% carbon cut plan",
                    rationale=f"Implied carbon price {p['implied_price_per_t']} per tonne",
                    evidence_refs=["tradeoff_frontier"], confidence=conf,
                    estimated_impact=f"+{p['extra_cost']:,} cost, {p['late_orders']} late orders",
                    requires_approval=True))
    if r and r.get("kpis"):
        late = [a["order_id"] for a in r["plan"]["allocation"] if not a["on_time"]]
        if late:
            recs.append(Recommendation(
                action=f"Contact the customers of {len(late)} late orders",
                rationale="These orders have no on-time option in any plant" if s["policy"] == "never_late"
                else "Late delivery accepted under the allow-late policy",
                evidence_refs=["solve_allocation"] + late[:5], confidence="high",
                estimated_impact=f"{len(late)} orders arrive after their due date", requires_approval=False))
        for u in r["plan"]["unmet"][:5]:
            co = u.get("closest_option") or {}
            recs.append(Recommendation(
                action=f"Decide how to serve {u['order_id']}: add capacity or agree a new date",
                rationale=u["reason"], evidence_refs=["solve_allocation", u["order_id"]], confidence="high",
                estimated_impact=(f"closest option Plant {co.get('plant')} arriving {co.get('arrival')}"
                                  if co else "no eligible plant"), requires_approval=True))
    return recs


@app.post("/analyze", response_model=AnalyzeResponse, response_model_exclude_none=True)
def analyze(req: AnalyzeRequest):
    t0 = time.perf_counter()
    w = (req.weights.model_dump(exclude_none=True) if req.weights else dict(cfg()["weights"]))
    policy = req.policy or cfg()["delivery_policy"]
    try:
        s = run_graph(req.request, w, policy, req.planning_start, req.scenario)
    except DataNotReady as e:
        raise HTTPException(409, str(e)) from e
    at = s.get("answer_type") or "allocation"
    g = s.get("gate", {})
    r = s.get("scen") or s.get("result")
    run_id = new_run_id()
    resp = {"run_id": run_id, "answer_type": at, "weights_used": s["weights"],
            "summary": g.get("message") or g.get("question") or s.get("text", ""),
            "assumptions": build_assumptions(s), "limitations": LIMITATIONS,
            "parent_run_id": s.get("parent_run_id")}
    if at == "clarification_needed":
        resp["clarification"] = {"question": g.get("question"), "options": g.get("options", [])}
    elif at == "refused":
        resp["clarification"] = {"alternative": g.get("alternative")}
    else:
        resp["status"] = _status(at, s)
        resp["number_check"] = s.get("number_check")
        resp["recommendations"] = _recommendations(at, s)
        if at == "brief":
            resp["brief"] = s.get("text")
            resp["kpis"] = s["brief_evidence"]["kpis"]
            resp["summary"] = s["text"].splitlines()[0].lstrip("# ") if s.get("text") else ""
            resp["comparison"] = s["brief_evidence"].get("comparison")
            resp["tradeoff"] = s["brief_evidence"].get("tradeoff")
            resp["explanation"] = {"sources": s["brief_evidence"]["sources"]}
        if r:
            resp.update(allocation=r["plan"]["allocation"], kpis=r.get("kpis"), validator=r.get("validator"),
                        unmet=r["plan"]["unmet"],
                        solver={k: r["plan"][k] for k in ("status", "backend", "solve_ms", "refs")})
        if s.get("tradeoff"):
            resp["tradeoff"] = s["tradeoff"]
        if s.get("comparison"):
            resp["comparison"] = s["comparison"]
            resp["baseline_kpis"] = s["result"]["kpis"]
        if s.get("explanation") and at != "brief":
            resp["explanation"] = s["explanation"]
    latency = round((time.perf_counter() - t0) * 1000)
    out = AnalyzeResponse(**resp)
    stored_plan = r["plan"] if r and at in ("allocation", "infeasible", "scenario") and not s.get("explanation") else None
    if s.get("tradeoff") and s["intent"].type == "tradeoff":
        stored_plan = None  # a trade-off run is not a new current plan
    ctx = s.get("ctx")
    save_trace(run_id, req.request, out.model_dump(mode="json"), s.get("steps", []), s.get("llm_calls", 0),
               s.get("tokens", 0), latency,
               {"datasets": datasets(), "weights": s["weights"], "policy": policy, "scenario": req.scenario,
                "planning_start": ctx.start if ctx else None, "events": ctx.events if ctx else []},
               plan=stored_plan, parent_run_id=s.get("parent_run_id"))
    return out
