"""Shared UI pieces: API client, health panel, status banners. The UI only talks to the FastAPI service."""
import os

import httpx
import streamlit as st

API = os.getenv("API_URL", "http://localhost:8000")
TIMEOUT = httpx.Timeout(180.0, connect=5.0)

STATUS = {
    "valid_on_time": ("success", "Valid and on time", "Every order meets its due date within capacity."),
    "valid_late_flagged": ("warning", "Valid · late orders flagged",
                           "The plan passes capacity and eligibility. Orders with no on-time option in any plant "
                           "are flagged, not hidden."),
    "infeasible": ("error", "Infeasible", "Some orders cannot be met. Each is listed with its closest option; "
                   "no plan is shown as successful."),
    "rejected_by_validator": ("error", "Rejected by validator",
                              "The solver's plan broke a rule. It is not shown as a plan; the trace shows which check failed."),
    "no_plan": ("info", "No plan", "No plan was produced for this request."),
}
BADGE = {"allocation": "blue", "scenario": "violet", "brief": "green", "infeasible": "red",
         "clarification_needed": "orange", "refused": "red"}


def api_get(path: str):
    try:
        r = httpx.get(f"{API}{path}", timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        st.error(f"API unreachable: cannot reach the planning service ({type(e).__name__}).")
        return None


def api_post(path: str, **kw):
    try:
        r = httpx.post(f"{API}{path}", timeout=TIMEOUT, **kw)
    except httpx.HTTPError as e:
        st.error(f"API unreachable: cannot reach the planning service ({type(e).__name__}).")
        return None
    if r.status_code >= 400:
        try:
            body = r.json()
        except ValueError:
            body = {"detail": r.text[:200]}
        return {"_error": r.status_code, **(body if isinstance(body, dict) else {"detail": body})}
    return r.json()


def settings() -> dict:
    s = st.session_state
    s.setdefault("cost_w", 70)
    s.setdefault("policy", "never_late")
    start = s.get("start")
    return {"weights": {"cost": s.cost_w / 100, "carbon": (100 - s.cost_w) / 100}, "policy": s.policy,
            **({"planning_start": start.isoformat()} if start else {})}


def analyze(request: str, scenario: dict | None = None, weights: dict | None = None):
    body = {"request": request, **settings()}
    if weights:
        body["weights"] = weights
    if scenario:
        body["scenario"] = scenario
    with st.spinner("Solving… stops at the time limit and returns the best valid plan found"):
        r = api_post("/analyze", json=body)
    if r and "_error" in r:
        st.error(r.get("detail") or r.get("error"))
        return None
    if r:
        st.session_state.setdefault("history", []).append((request, r))
        st.session_state[f"last_{r['answer_type']}"] = r
        if r["answer_type"] in ("allocation", "infeasible") and r.get("allocation") and not r.get("explanation"):
            st.session_state["plan"] = r
    return r


def sidebar_health():
    h = api_get("/health") if "health" not in st.session_state else st.session_state.health
    with st.sidebar:
        st.caption("System health · GET /health")
        if not h:
            st.markdown(":red[● API unreachable]")
            return
        st.markdown(f":green[●] API {h['api']}")
        ds = h["data_store"]
        st.markdown(f"{':green[●]' if ds['status'] == 'ready' else ':red[●]'} SQLite {ds['status']}")
        llm = h["llm"]
        dot = ":green[●]" if llm["status"] == "configured" else ":orange[●]"
        st.markdown(f"{dot} LLM: {llm['model']}")
        if llm["status"] == "mock":
            st.caption("LLM off: explanations use templates built from the computed numbers.")


def status_banner(resp: dict):
    kind, title, text = STATUS.get(resp.get("status") or "no_plan", STATUS["no_plan"])
    v = resp.get("validator") or {}
    checks = " · ".join(f"{k.replace('_ok', '')} {'ok' if v.get(k) else 'FAILED'}"
                        for k in ("capacity_ok", "eligibility_ok", "delivery_ok") if k in v)
    getattr(st, kind)(f"**{title}** — {text}" + (f"  \n{checks}" if checks else ""))


def answer_badge(at: str) -> str:
    return f":{BADGE.get(at, 'gray')}-background[{at}]"


def number_check_badge(resp: dict):
    nc = resp.get("number_check")
    if not nc:
        return
    if nc.get("passed"):
        st.caption("Verified: every figure matches a tool result" + (" (template fallback)" if nc.get("fallback") else ""))
    else:
        st.warning(f"Number check failed: {', '.join(nc.get('unmatched', []))}")


def fmt(v, unit: str = "") -> str:
    if v is None:
        return "—"
    return (f"{v:,.0f}" if isinstance(v, (int, float)) and abs(v) >= 100 else f"{v:g}") + unit


def assumptions_box(resp: dict):
    with st.expander("Assumptions and limitations"):
        for a in resp.get("assumptions", []):
            st.markdown(f"- {a}")
        for lim in resp.get("limitations", []):
            st.markdown(f"- *Limitation:* {lim}")
