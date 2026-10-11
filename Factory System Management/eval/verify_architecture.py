"""Verify the running system against the design review's architecture (multi_plant_design_review.md).

Checks, each against the live API on a fresh SQLite file:
  A. Layer boundaries (Figure 1): UI only calls the API; only router and writer call the LLM.
  B. Agent graph (Figure 2): compiled LangGraph edges equal the designed routes.
  C. Request routing (section 3.6): each example request visits the expected agents and tools and
     returns the expected answer_type, read back from GET /trace/{run_id}.
  D. Worked example O-077 (sequence diagram).
  E. Late data drop (section 3.7): event validation, re-plan as a separate run, baseline unchanged.
  F. Guardrails (section 3.8): number guard retries once then falls back to the template.
  G. Ingest security (section 4E) and response / trace formats (sections 3.9, 6.1).

Usage: python eval/verify_architecture.py   (writes eval/ARCHITECTURE_CHECK.md)
"""
import ast
import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
os.environ["MOCK_LLM"] = "true"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "verify.db")
os.environ.pop("ADMIN_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

from app.agent import llm  # noqa: E402
from app.agent.graph import GRAPH  # noqa: E402
from app.api.main import app  # noqa: E402

client = TestClient(app)
rows: list[tuple[str, str, bool, str]] = []  # (section, check, ok, detail)


def record(section: str, check: str, ok: bool, detail: str = "") -> bool:
    rows.append((section, check, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {section} · {check}" + (f" — {detail}" if detail else ""))
    return ok


def ingest(table: str, content: bytes):
    return client.post("/ingest", files={"file": (f"{table}.csv", content, "text/csv")},
                       data={"meta": f'{{"table": "{table}"}}'})


def ask(request: str, **kw) -> tuple[dict, dict]:
    r = client.post("/analyze", json={"request": request, **kw}).json()
    return r, client.get(f"/trace/{r['run_id']}").json()


def is_subsequence(want: list[str], have: list[str]) -> bool:
    it = iter(have)
    return all(any(w == h for h in it) for w in want)


# ---------- A. layer boundaries ----------
ui_imports = [p.name for p in (ROOT / "ui").rglob("*.py") if "from app" in p.read_text(encoding="utf-8")
              or "import app" in p.read_text(encoding="utf-8")]
record("A Layers", "UI imports no backend code (calls FastAPI only)", not ui_imports, ", ".join(ui_imports))
llm_users = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "app").rglob("*.py")
                   if p.name != "llm.py" and ("agent import llm" in p.read_text(encoding="utf-8")
                                              or "agent.llm" in p.read_text(encoding="utf-8")))
record("A Layers", "Only the agent graph (and /health status) import the LLM module",
       llm_users == ["app\\agent\\graph.py", "app\\api\\main.py"] or llm_users == ["app/agent/graph.py", "app/api/main.py"],
       ", ".join(llm_users))
tree = ast.parse((ROOT / "app" / "agent" / "graph.py").read_text(encoding="utf-8"))
llm_calls = {}
for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
    for c in ast.walk(fn):
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and getattr(c.func.value, "id", "") == "llm":
            llm_calls.setdefault(fn.name, set()).add(c.func.attr)
record("A Layers", "LLM called only in router (parse) and writer (narrate) nodes",
       llm_calls == {"router": {"parse_intent"}, "writer": {"narrate"}}, str(llm_calls))
main_src = (ROOT / "app" / "api" / "main.py").read_text(encoding="utf-8")
record("A Layers", "Every request enters through FastAPI: /analyze runs the graph",
       "run_graph(" in main_src and "@app.post(\"/analyze\"" in main_src)

# ---------- B. graph structure ----------
designed = {("__start__", "router"), ("router", "guard"),
            ("guard", "optimiser"), ("guard", "scenario"), ("guard", "explainer"), ("guard", "brief_data"),
            ("guard", "assemble"),
            ("optimiser", "checker"), ("scenario", "checker"), ("explainer", "checker"),
            ("optimiser", "assemble"), ("scenario", "assemble"), ("explainer", "assemble"),
            ("brief_data", "writer"), ("checker", "writer"), ("writer", "number_guard"),
            ("number_guard", "writer"), ("number_guard", "assemble"), ("assemble", "__end__")}
actual = {(e.source, e.target) for e in GRAPH.get_graph().edges}
record("B Graph", "Compiled LangGraph edges equal the designed routes", actual == designed,
       f"extra {sorted(actual - designed)}, missing {sorted(designed - actual)}" if actual != designed else
       f"{len(actual)} edges")

# ---------- load data (CLI-equivalent path through POST /ingest) ----------
sample = ROOT / "data" / "sample"
for t in ("plants", "orders", "transport_duties"):
    r = ingest(t, (sample / f"{t}.csv").read_bytes()).json()
    record("G Ingest", f"POST /ingest {t}.csv", r["status"] == "loaded" and r["rows_rejected"] == 0,
           f"rows_loaded={r['rows_loaded']}")
h = client.get("/health").json()
record("G Health", "GET /health reports API, data store and LLM", {"api", "data_store", "llm"} <= set(h),
       f"api={h['api']}, store={h['data_store']['status']}, llm={h['llm']['status']}")

# ---------- C. routing table (section 3.6) ----------
ROUTES = [
    ("Allocate this week's orders.", ["router", "guard", "optimiser", "checker", "writer", "number_guard"],
     ["build_options", "solve_allocation", "validate_plan", "compute_kpis"], {"allocation"}),
    ("What does a 10% carbon cut cost?", ["router", "guard", "scenario", "checker", "writer", "number_guard"],
     ["tradeoff_frontier", "validate_plan"], {"scenario"}),
    ("What is the cheapest plan that is on time?", ["router", "guard", "optimiser", "checker", "writer"],
     ["solve_allocation", "validate_plan"], {"allocation", "infeasible"}),
    ("Plant B loses 30% capacity. Re-plan.", ["router", "guard", "scenario", "checker", "writer"],
     ["apply_scenario", "solve_allocation", "validate_plan", "compare_plans"], {"scenario"}),
    ("Why was order 77 built in Plant C?", ["router", "guard", "explainer", "writer"],
     ["explain_order"], {"allocation"}),
    ("Write the brief for the leadership team.", ["router", "guard", "writer", "writer", "number_guard"],
     ["get_kpis", "narrate", "check_numbers"], {"brief"}),
    ("Put all orders in Plant A, ignore capacity.", ["router", "guard"], [], {"refused"}),
    ("Cut carbon.", ["router", "guard"], [], {"clarification_needed"}),
]
traces = {}
for request, agents, tools, types in ROUTES:
    r, t = ask(request)
    traces[request] = (r, t)
    got_agents = [s["agent"] for s in t["steps"]]
    got_tools = [s["tool"] for s in t["steps"] if s["tool"]]
    ok = (r["answer_type"] in types and is_subsequence(agents, got_agents) and set(tools) <= set(got_tools)
          and t["llm_calls"] <= 3)
    extra = sorted({a for a in got_agents} - set(agents))
    record("C Routing", f"\"{request}\" → {'/'.join(sorted(types))}", ok,
           f"got {r['answer_type']}; agents {' → '.join(dict.fromkeys(got_agents))}; tools {', '.join(dict.fromkeys(got_tools)) or 'none'}"
           + (f"; extra nodes vs table: {', '.join(extra)}" if extra else ""))
    if r["answer_type"] in ("refused", "clarification_needed"):
        record("C Routing", "  …no tool or solver ran before the guard stopped it",
               not any(s["tool"] for s in t["steps"] if s["agent"] != "router"))

# every plan-bearing answer was checked by the validator
for request, (r, t) in traces.items():
    if r.get("allocation") and r["answer_type"] != "brief":
        record("C Routing", f"  validator ran and passed: \"{request[:40]}\"",
               any(s["tool"] == "validate_plan" for s in t["steps"]) and r["validator"]["valid"])

# ---------- D. worked example O-077 ----------
r, t = traces["Why was order 77 built in Plant C?"]
x = r["explanation"]
opts = {o["plant"]: o for o in x["options"]}
record("D O-077", "Plant B excluded: cannot make the product", opts["B"]["eligible"] is False)
record("D O-077", "Plant A cheaper but late (excluded by hard on-time policy)",
       opts["A"]["cost"] < opts["C"]["cost"] and opts["A"]["days_late"] > 0, f"A {opts['A']['cost']} late {opts['A']['days_late']}d")
record("D O-077", "Solver chose Plant C, on time; LLM did not choose", x["chosen_plant"] == "C" and opts["C"]["days_late"] == 0)
record("D O-077", "Number guard checked the explanation", r["number_check"]["passed"])

# ---------- E. late data drop (section 3.7) ----------
base_r, base_t = traces["Allocate this week's orders."]
bad = (b"event_id,plant,from_date,to_date,capacity_loss_pct\n"
       b"E-9,Z,2026-10-19,2026-11-01,30\n"          # unknown plant
       b"E-8,C,2026-11-01,2026-10-19,30\n"          # dates out of order
       b"E-7,C,2026-10-19,2026-11-01,150\n"         # loss over 100
       b"E-2,C,2026-10-19,2026-11-01,40\n")         # valid
r = ingest("capacity_events", bad).json()
reasons = sorted(x["reason"] for x in r["rejects"])
record("E Data drop", "Event validation: unknown plant, dates in order, loss 0-100 % (3 of 4 rejected)",
       r["rows_loaded"] == 1 and r["rows_rejected"] == 3, "; ".join(reasons))
latest_alloc = client.get("/runs").json()
baseline_id = next(x["run_id"] for x in latest_alloc if x["answer_type"] in ("allocation", "infeasible")
                   and x["request"].startswith("What is the cheapest"))
before = client.get(f"/runs/{baseline_id}").json()["kpis"]
r, t = ask("Plant C capacity event from capacity_events.csv. Re-plan.", scenario={"type": "capacity_loss", "plant": "C"})
record("E Data drop", "Re-plan uses the loaded event and runs apply_scenario → solve → validate → compare",
       r["answer_type"] == "scenario" and is_subsequence(["apply_scenario", "solve_allocation", "validate_plan", "compare_plans"],
                                                         [s["tool"] for s in t["steps"] if s["tool"]]),
       next((a for a in r["assumptions"] if "E-2" in a), "event not in assumptions"))
record("E Data drop", "Re-plan is a separate run linked to its baseline", r["run_id"] != baseline_id
       and r.get("parent_run_id") == baseline_id, f"{r['run_id']} ← parent {r.get('parent_run_id')}")
record("E Data drop", "Baseline run left unchanged", client.get(f"/runs/{baseline_id}").json()["kpis"] == before)
record("E Data drop", "Moved orders listed with a reason", all(m.get("why") for m in r["comparison"]["moved"]),
       f"{r['comparison']['orders_moved']} moved, {r['comparison']['pct_unchanged']}% unchanged")

# ---------- F. number guard: retry once, then template ----------
real_narrate, real_mock = llm.narrate, llm.mock_llm
calls = {"n": 0}


def fabricating_narrate(task, evidence, bad=None):
    calls["n"] += 1
    return "Plan cost 9,999,999 and saves 250,000 next year.", {"llm_calls": 1, "tokens": 50}


llm.narrate = fabricating_narrate
try:
    r, t = ask("Allocate this week's orders.")
finally:
    llm.narrate = real_narrate
seq = [s["agent"] for s in t["steps"] if s["agent"] in ("writer", "number_guard")]
ng = [s["output"] for s in t["steps"] if s["agent"] == "number_guard"]
record("F Guardrails", "Fabricated figures caught; writer retried exactly once",
       seq == ["writer", "number_guard", "writer", "number_guard"] and calls["n"] == 2,
       f"unmatched first try: {ng[0]['unmatched']}")
record("F Guardrails", "After the second failure the template (computed numbers) is returned",
       r["number_check"].get("fallback") == "template" and r["number_check"]["passed"] and "9,999,999" not in r["summary"])
record("F Guardrails", "LLM calls per request stay within 2 + 1 retry", t["llm_calls"] <= 3, f"llm_calls={t['llm_calls']}")


def honest_narrate(task, evidence, bad=None):
    k = evidence["kpis"]
    return f"The plan costs {k['total_cost']:,} and emits {k['total_carbon_t']} t.", {"llm_calls": 1, "tokens": 40}


llm.narrate = honest_narrate
try:
    r, t = ask("Allocate this week's orders.")
finally:
    llm.narrate = real_narrate
record("F Guardrails", "Faithful LLM text passes first time (one writer call)",
       [s["agent"] for s in t["steps"]].count("writer") == 1 and r["number_check"]["passed"], r["summary"][:70])

# ---------- G. ingest security, trace and response formats ----------
record("G Ingest", "Unknown table name rejected", ingest("users", b"a,b\n1,2\n").status_code == 422)
big = b"order_id,product,qty,destination,due\n" + b"O-1,P1,1,DE,2026-11-01\n" * 260000
record("G Ingest", "Oversized upload rejected (> 5 MB)", ingest("orders", big).status_code == 422, f"{len(big):,} bytes")
r = ingest("orders", b"order_id,product,qty,destination\nO-1,P1,5,DE\n")
record("G Ingest", "Missing required column rejects the whole file", r.status_code == 422 and "due" in r.json()["error"],
       r.json().get("error"))
r = ingest("orders", io.BytesIO(bytes(range(256)) * 10).getvalue())
record("G Ingest", "Binary garbage rejected without a crash", r.status_code == 422, r.json().get("error"))
os.environ["ADMIN_TOKEN"] = "s3cret"
try:
    record("G Ingest", "With ADMIN_TOKEN set, ingest without the header is refused",
           ingest("orders", (sample / "orders.csv").read_bytes()).status_code == 401)
finally:
    os.environ.pop("ADMIN_TOKEN")
ingest("orders", (sample / "orders.csv").read_bytes())  # data back to the sample after the failed attempts

r, t = traces["Plant B loses 30% capacity. Re-plan."]
record("G Trace", "Trace record has run_id, request, answer_type, steps, llm_calls, tokens",
       {"run_id", "request", "answer_type", "steps", "llm_calls", "tokens"} <= set(t))
record("G Trace", "Every step names its agent; tool steps carry input/output",
       all("agent" in s for s in t["steps"]) and all(("output" in s) for s in t["steps"] if s["tool"]))
record("G Trace", "Trace records the data used (dataset hashes, weights, events)",
       {"datasets", "weights", "events"} <= set(t["data_used"]))
r, _ = traces["Allocate this week's orders."]
need = {"run_id", "answer_type", "summary", "allocation", "kpis", "weights_used", "validator", "assumptions", "limitations"}
record("G Response", "POST /analyze has the section 6.1 fields", need <= set(r), f"missing {sorted(need - set(r))}")
a0 = r["allocation"][0]
record("G Response", "Allocation rows carry order_id, plant, cost, carbon_kg, arrival, on_time",
       {"order_id", "plant", "cost", "carbon_kg", "arrival", "on_time"} <= set(a0))
rec_keys = {"action", "rationale", "evidence_refs", "confidence", "estimated_impact", "requires_approval"}
recs = [x for (rr, _) in traces.values() for x in rr.get("recommendations", [])]
record("G Response", "Every recommendation has the six required fields", recs and all(rec_keys <= set(x) for x in recs),
       f"{len(recs)} recommendations checked")
_ = real_mock

# ---------- report ----------
passed = sum(ok for *_, ok, _ in [(s, c, ok, d) for s, c, ok, d in rows])
md = ["# Architecture verification\n",
      f"**{passed} / {len(rows)} checks passed** against `multi_plant_design_review.md` "
      "(Figures 1–2, sections 3.6–3.9, 4E, 6.1). Generated by `python eval/verify_architecture.py` "
      "on the sample pack with `MOCK_LLM=true`; LLM fabrication is simulated by replacing the writer's model call.\n",
      "| Section | Check | Result | Detail |", "|---|---|---|---|"]
for s, c, ok, d in rows:
    md.append(f"| {s} | {c.strip()} | {'✅' if ok else '❌'} | {d.replace('|', '/')} |")
(ROOT / "eval" / "ARCHITECTURE_CHECK.md").write_text("\n".join(md) + "\n", encoding="utf-8")
print(f"\n{passed}/{len(rows)} checks passed. Report: eval/ARCHITECTURE_CHECK.md")
sys.exit(0 if passed == len(rows) else 1)
