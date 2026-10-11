import pandas as pd
import streamlit as st
from common import analyze, api_get, fmt, number_check_badge, status_banner

base = st.session_state.get("plan")
st.caption(f"SCENARIO · BASELINE KEPT AS RUN {base['run_id'].upper()}" if base else "SCENARIO")
st.title("Re-plan after capacity loss")

status = api_get("/data/status") or {"datasets": {}}
has_events = "capacity_events" in status["datasets"] and status["datasets"]["capacity_events"]["rows_loaded"]

left, right = st.columns([1, 2.2])
with left, st.container(border=True):
    st.markdown("**Disruption**")
    st.selectbox("Scenario type", ["Capacity loss"])
    src = st.selectbox("Source", (["Event from capacity_events.csv"] if has_events else []) + ["Manual"])
    plant = st.selectbox("Plant", ["A", "B", "C"], index=1)
    manual = src == "Manual"
    pct = st.number_input("Capacity lost (%)", 1, 100, 30, disabled=not manual)
    f = st.date_input("From", value=None, disabled=not manual)
    t = st.date_input("To", value=None, disabled=not manual)
    if st.button("Run re-plan", type="primary", use_container_width=True):
        if manual:
            sc = {"type": "capacity_loss", "plant": plant, "capacity_loss_pct": pct,
                  **({"from_date": f.isoformat()} if f else {}), **({"to_date": t.isoformat()} if t else {})}
            req = f"Plant {plant} loses {pct}% capacity. Re-plan."
        else:
            sc = {"type": "capacity_loss", "plant": plant}
            req = f"Plant {plant} capacity event from capacity_events.csv. Re-plan."
        r = analyze(req, scenario=sc)
        if r:
            st.session_state["replan_resp"] = r
    st.caption("Capacity drops only for the chosen plant in the weeks the event covers (pro rata by day). "
               "Orders move only when they must: moving an order carries a small penalty.")

r = st.session_state.get("replan_resp")
with right:
    if not r:
        st.info("Run a re-plan to compare it with the baseline. The baseline is never overwritten.")
        st.stop()
    if r["answer_type"] == "clarification_needed":
        st.warning(r["summary"])
        st.stop()
    status_banner(r)
    st.caption(f"run {r['run_id']} · baseline {r.get('parent_run_id') or 'solved in this run'}")
    c = r.get("comparison") or {}
    if c:
        m = st.columns(4)
        m[0].metric("Landed cost", f"{c['extra_cost']:+,}", help=f"{fmt(c['cost_before'])} → {fmt(c['cost_after'])}")
        m[0].caption(f"{fmt(c['cost_before'])} → {fmt(c['cost_after'])}")
        m[1].metric("Carbon", f"{c['extra_carbon_t']:+g} t")
        m[1].caption(f"{c['carbon_before_t']} t → {c['carbon_after_t']} t")
        m[2].metric("Late orders", f"{c['delta_late_orders']:+d}")
        m[2].caption(f"{c['late_orders_before']} → {c['late_orders_after']}")
        m[3].metric("Orders moved", c["orders_moved"])
        m[3].caption(f"{c['pct_unchanged']}% of orders unchanged")

if c:
    with st.container(border=True):
        st.markdown("**Orders that moved**")
        mv = pd.DataFrame(c["moved"])
        if mv.empty:
            st.caption("No order had to move.")
        else:
            st.dataframe(mv, hide_index=True, use_container_width=True)
            st.download_button("Download CSV", mv.to_csv(index=False), "moved_orders.csv")
    a, b = st.columns(2)
    with a, st.container(border=True):
        st.markdown("**What changed and why**")
        st.write(r["summary"])
        number_check_badge(r)
    with b, st.container(border=True):
        recs = [x for x in r.get("recommendations", []) if x["requires_approval"]]
        st.markdown("**Recommendation** · :orange-background[Requires approval]")
        if recs:
            x = recs[0]
            st.markdown(f"**Action** {x['action']}  \n**Estimated impact** {x['estimated_impact']}  \n"
                        f"**Confidence** {x['confidence']}  \n**Evidence** {', '.join(x['evidence_refs'])} step in the trace")
        b1, b2 = st.columns(2)
        if b1.button("Approve re-plan", type="primary"):
            st.session_state["approved_replan"] = r["run_id"]
        if b2.button("Keep baseline"):
            st.session_state["approved_replan"] = None
        if st.session_state.get("approved_replan") == r["run_id"]:
            st.success(f"Approved: run {r['run_id']} recorded as the plan to adopt for the affected weeks.")
