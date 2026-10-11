"""Run eval/cases.yaml against the API and write eval/REPORT.md.

Usage: python eval/run_eval.py [--backend cpsat|pulp] [--out eval/REPORT.md]
Runs in-process through FastAPI's TestClient on a fresh SQLite file, so results are reproducible.
LLM mode follows MOCK_LLM / LLM_PROVIDER / LLM_MODEL from the environment.
"""
import argparse
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "eval.db"))

import pandas as pd  # noqa: E402
import yaml  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.agent.guard import check_numbers  # noqa: E402
from app.api.main import app  # noqa: E402
from app.data.store import ingest_bytes  # noqa: E402
from app.models.options import capacity_table  # noqa: E402
from app.solver.milp import greedy_baseline  # noqa: E402
from app.solver.validator import validate_plan  # noqa: E402
from app.tools.kpis import compute_kpis  # noqa: E402
from app.tools.planner import make_ctx  # noqa: E402

SAMPLE = ROOT / "data" / "sample"


def load_dataset(spec: dict | None) -> None:
    for table in ("plants", "orders", "transport_duties", "capacity_events"):
        df = pd.read_csv(SAMPLE / f"{table}.csv", dtype=str, keep_default_na=False)
        if table == "orders" and spec and spec.get("extra_orders"):
            df = pd.concat([df, pd.DataFrame(spec["extra_orders"]).astype(str)], ignore_index=True)
        ingest_bytes(df.to_csv(index=False).encode(), table)


def check(case: dict, r: dict, results: dict) -> list[str]:
    e, fails = case["expect"], []
    alloc = {a["order_id"]: a for a in r.get("allocation", [])}
    k = r.get("kpis") or {}

    def need(cond, msg):
        if not cond:
            fails.append(msg)

    if "answer_type" in e:
        need(r["answer_type"] == e["answer_type"], f"answer_type {r['answer_type']} != {e['answer_type']}")
    if "answer_type_in" in e:
        need(r["answer_type"] in e["answer_type_in"], f"answer_type {r['answer_type']} not in {e['answer_type_in']}")
    if "valid" in e:
        need((r.get("validator") or {}).get("valid") == e["valid"], f"validator valid != {e['valid']}")
    if "late_orders" in e:
        need(k.get("late_orders") == e["late_orders"], f"late_orders {k.get('late_orders')} != {e['late_orders']}")
    if "unmet" in e:
        need(len(r.get("unmet", [])) == e["unmet"], f"unmet {len(r.get('unmet', []))} != {e['unmet']}")
    for o in e.get("on_time", []):
        need(alloc.get(o, {}).get("on_time") is True, f"{o} not on time")
    for o, p in (e.get("plant_not") or {}).items():
        need(alloc.get(o, {}).get("plant") not in (None, p), f"{o} in plant {alloc.get(o, {}).get('plant')}")
    for o, p in (e.get("plant_is") or {}).items():
        need(alloc.get(o, {}).get("plant") == p, f"{o} in plant {alloc.get(o, {}).get('plant')} != {p}")
    if "late_ids" in e:
        late = sorted(o for o, a in alloc.items() if not a["on_time"])
        need(late == sorted(e["late_ids"]), f"late ids {late}")
    if "weights_used" in e:
        need(r["weights_used"].get("cost") == e["weights_used"]["cost"], f"weights_used {r['weights_used']}")
    x = r.get("explanation") or {}
    if "chosen_plant" in e:
        need(x.get("chosen_plant") == e["chosen_plant"], f"chosen {x.get('chosen_plant')}")
    if "reason_contains" in e:
        need(e["reason_contains"] in (x.get("binding_reason") or ""), f"reason '{x.get('binding_reason')}'")
    ref = lambda cid: (results.get(cid) or {}).get("resp", {}).get("kpis") or {}  # noqa: E731
    if "cost_le_ref" in e:
        need(k.get("total_cost", 1e18) <= ref(e["cost_le_ref"]).get("total_cost", 0), "cost not <= reference plan")
    if "carbon_lt_ref" in e:
        need(k.get("total_carbon_t", 1e18) < ref(e["carbon_lt_ref"]).get("total_carbon_t", 0), "carbon not < reference plan")
    if "plan_differs_from" in e:
        other = {a["order_id"]: a["plant"] for a in results[e["plan_differs_from"]]["resp"].get("allocation", [])}
        need(any(other.get(o) != a["plant"] for o, a in alloc.items()), "plan identical to reference")
    t = {p["carbon_cut_pct"]: p for p in r.get("tradeoff") or []}
    if "cut_feasible" in e:
        need(t.get(e["cut_feasible"], {}).get("feasible"), f"{e['cut_feasible']}% cut not feasible")
    if "cut_costs_more" in e:
        need((t.get(e["cut_costs_more"], {}).get("extra_cost") or -1) > 0, "carbon cut did not add cost")
    if e.get("frontier_monotone"):
        pts = [p for p in sorted(t.values(), key=lambda p: p["carbon_cut_pct"]) if p["feasible"]]
        need(len(pts) >= 3 and all(a["cost"] <= b["cost"] + 1 for a, b in zip(pts, pts[1:], strict=False)), "frontier not monotone")
    c = r.get("comparison") or {}
    if e.get("not_better_on_both"):  # less capacity cannot make the plan cheaper AND greener
        need(not (c.get("extra_cost", 0) < 0 and c.get("extra_carbon_t", 0) < 0),
             f"re-plan better on both: {c.get('extra_cost')}, {c.get('extra_carbon_t')} t")
    if "moved_ge" in e:
        need(c.get("orders_moved", 0) + c.get("orders_rescheduled", 0) >= e["moved_ge"], "no orders moved")
    for o in e.get("unmet_includes", []):
        need(o in {u["order_id"] for u in r.get("unmet", [])}, f"{o} not reported unmet")
    nc = r.get("number_check") or {}
    if e.get("numbers_pass") or (e.get("numbers_pass_if_brief") and r["answer_type"] == "brief"):
        need(nc.get("passed") is True, f"number check {nc}")
    if e.get("brief_cost_matches_kpi"):
        need(f"{k.get('total_cost', 0):,}" in (r.get("brief") or ""), "brief cost != KPI total_cost")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "eval" / "REPORT.md"))
    a = ap.parse_args()
    cases = yaml.safe_load((ROOT / "eval" / "cases.yaml").read_text(encoding="utf-8"))["cases"]
    client = TestClient(app)
    results, current = {}, "unset"
    lat, calls, tokens = [], [], []
    for case in cases:
        if "check_numbers" in case:
            cn = case["check_numbers"]
            r = check_numbers(cn["text"], {"kpis": {"total_cost": 1659698}})
            ok = not r["passed"] and cn["expect_fail"] in r["unmatched"]
            results[case["id"]] = {"case": case, "pass": ok, "fails": [] if ok else [str(r)], "ms": 0, "resp": {}}
            continue
        ds = repr(case.get("dataset"))
        if ds != current:
            load_dataset(case.get("dataset"))
            current = ds
        body = {"request": case["request"]}
        for key in ("weights", "scenario", "policy"):
            if key in case:
                body[key] = case[key]
        t0 = time.perf_counter()
        resp = client.post("/analyze", json=body)
        ms = (time.perf_counter() - t0) * 1000
        r = resp.json()
        fails = [f"HTTP {resp.status_code}: {r}"] if resp.status_code != 200 else check(case, r, results)
        if resp.status_code == 200:
            tr = client.get(f"/trace/{r['run_id']}").json()
            calls.append(tr["llm_calls"])
            tokens.append(tr["tokens"])
        lat.append(ms)
        results[case["id"]] = {"case": case, "pass": not fails, "fails": fails, "ms": ms, "resp": r}
        print(f"{'PASS' if not fails else 'FAIL'} {case['id']:34} {r.get('answer_type', '?'):22} {ms:7.0f} ms  {'; '.join(fails)}")

    # plan-level metrics on the sample data
    load_dataset(None)
    ctx = make_ctx()
    greedy = greedy_baseline(ctx.options, ctx.tables["orders"], ctx.caps, ctx.cap_mode)
    gv = validate_plan(greedy, ctx.tables, ctx.start, "never_late")
    gk = compute_kpis(greedy, ctx.caps)
    gk_allow = validate_plan(greedy, ctx.tables, ctx.start, "allow_late")
    from app.tools.planner import run_plan
    configs = {}
    for backend in ("cpsat", "pulp"):
        t0 = time.perf_counter()
        rr = run_plan(ctx, {"cost": 0.7, "carbon": 0.3}, "never_late", backend=backend)
        configs[backend] = {"status": rr["plan"]["status"], "kpis": rr["kpis"], "valid": rr["validator"]["valid"],
                            "ms": (time.perf_counter() - t0) * 1000}
    _ = capacity_table  # imported for readers: capacity source used by both plans

    plan_cases = [x for x in results.values() if x["resp"].get("validator")]
    valid_rate = sum(x["resp"]["validator"]["valid"] for x in plan_cases) / max(1, len(plan_cases))
    briefs = [x for x in results.values() if x["resp"].get("answer_type") == "brief"]
    faithful = sum((x["resp"].get("number_check") or {}).get("passed", False) for x in briefs)
    rp = [x["resp"]["comparison"] for x in results.values() if x["case"].get("category") == "capacity_loss"
          and x["resp"].get("comparison")]
    q = sorted(lat)
    p95 = q[min(len(q) - 1, int(round(0.95 * (len(q) - 1))))]
    passed = sum(x["pass"] for x in results.values())
    base = results["alloc-baseline"]["resp"]["kpis"]

    md = ["# Evaluation report\n",
          f"Cases: **{passed} / {len(results)} passed**. Method: exact match on `answer_type` and the per-case checks in "
          f"`eval/cases.yaml` (no LLM-as-judge). LLM mode: `{'mock' if os.getenv('MOCK_LLM', 'true') == 'true' else os.getenv('LLM_MODEL')}`.\n",
          "## Results by case\n", "| Case | Category | answer_type | Pass | Latency ms | Notes |", "|---|---|---|---|---|---|"]
    for cid, x in results.items():
        md.append(f"| {cid} | {x['case'].get('category')} | {x['resp'].get('answer_type', '—')} | "
                  f"{'✅' if x['pass'] else '❌'} | {x['ms']:.0f} | {'; '.join(x['fails'])} |")
    md += ["\n## Metrics\n", "| Metric | Value |", "|---|---|",
           f"| Plan validity (validator on every plan returned) | {valid_rate:.0%} of {len(plan_cases)} plans |",
           f"| Solver plan, 70/30 (cost / carbon / late) | {base['total_cost']:,} / {base['total_carbon_t']} t / {base['late_orders']} |",
           f"| Greedy cheapest-plant plan (cost / carbon / late) | {gk['total_cost']:,} / {gk['total_carbon_t']} t / {gk['late_orders']} |",
           f"| Greedy plan passes the never-late validator? | {'yes' if gv['valid'] else 'no'} — {len(gv['violations'])} violations "
           f"(capacity ok: {gk_allow['capacity_ok']}) |",
           "| Re-plan quality (Δcost, Δcarbon, % orders unchanged) | " + "; ".join(
               f"{c['extra_cost']:+,} / {c['extra_carbon_t']:+g} t / {c['pct_unchanged']}%" for c in rp) + " |",
           f"| Brief faithfulness | {faithful} / {len(briefs)} briefs pass the number check |",
           f"| Latency p50 / p95 | {statistics.median(lat):.0f} ms / {p95:.0f} ms |",
           f"| LLM calls per request (mean) | {statistics.mean(calls):.2f} |",
           f"| Tokens per request (mean) | {statistics.mean(tokens):.0f} |",
           "\n## Configuration comparison: solver backend (70/30, never late)\n",
           "| Backend | Status | Total cost | Carbon t | Valid | Solve ms |", "|---|---|---|---|---|---|"]
    for b, c in configs.items():
        md.append(f"| {b} | {c['status']} | {c['kpis']['total_cost']:,} | {c['kpis']['total_carbon_t']} | {c['valid']} | {c['ms']:.0f} |")
    md.append("\nChoice: CP-SAT is the default backend; see the timings above. CBC is kept as a cross-check of the optimum.")
    Path(a.out).write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\n{passed}/{len(results)} passed. Report: {a.out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
