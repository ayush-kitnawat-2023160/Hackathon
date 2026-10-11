import pandas as pd
import streamlit as st
from common import analyze

r = st.session_state.get("last_brief")
src = (r or {}).get("explanation", {}).get("sources", {}) if r else {}
st.caption("ANSWER_TYPE: BRIEF" + (f" · BUILT FROM RUNS {', '.join(sorted(set(src.values()))).upper()}" if src else ""))
h1, h2, h3 = st.columns([4, 1, 1.4])
h1.title("Management brief")
if h2.button("Regenerate" if r else "Write brief", type="primary"):
    r = analyze("Write the brief for the leadership team.")
if not r:
    st.info("Write the brief once a plan exists. It uses the latest plan, trade-off and re-plan runs.")
    st.stop()
h3.download_button("Download Markdown", r.get("brief") or "", f"brief_{r['run_id']}.md")

nc = r.get("number_check") or {}
if nc.get("passed"):
    st.success("**Number check passed** — every figure in the brief matches a tool result."
               + (" (Template brief: the model's text failed the check twice.)" if nc.get("fallback") else ""))
else:
    st.error(f"Number check failed: {', '.join(nc.get('unmatched', []))}")

left, right = st.columns([2, 1])
with left, st.container(border=True):
    st.markdown(r.get("brief") or r["summary"])
    st.caption("Assumptions: " + "; ".join(r.get("assumptions", [])[:2]) + ". Limitations: "
               + "; ".join(r.get("limitations", [])[:1]) + ".")
with right, st.container(border=True):
    st.markdown("**Where each figure comes from**")
    k, c = r.get("kpis") or {}, r.get("comparison") or {}
    rows = []
    if k:
        top = max(k["plant_share"], key=k["plant_share"].get)
        rows += [(f"{k['total_cost']:,}", "compute_kpis", src.get("kpis")),
                 (f"{k['total_carbon_t']} t", "compute_kpis", src.get("kpis")),
                 (str(k["late_orders"]), "compute_kpis", src.get("kpis")),
                 (f"{round(k['plant_share'][top] * 100)}%", "compute_kpis", src.get("kpis")),
                 (f"{round(k['plant_utilisation'][top] * 100)}%", "compute_kpis", src.get("kpis"))]
    for p in r.get("tradeoff") or []:
        if p["carbon_cut_pct"] > 0 and p["feasible"]:
            rows.append((f"{p['extra_cost']:,}", "tradeoff_frontier", src.get("tradeoff")))
            break
    if c:
        rows += [(f"{c['extra_cost']:,}", "compare_plans", src.get("comparison")),
                 (f"{c['extra_carbon_t']} t", "compare_plans", src.get("comparison"))]
    st.dataframe(pd.DataFrame(rows, columns=["Figure", "Tool", "Run"]), hide_index=True, use_container_width=True)
    st.caption("If any figure fails the check, the brief is rewritten once; if it fails again, a template "
               "brief built straight from the KPIs is shown instead.")
