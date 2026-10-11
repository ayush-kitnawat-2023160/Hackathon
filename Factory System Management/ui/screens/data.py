import pandas as pd
import streamlit as st
from common import analyze, api_get, api_post, settings

st.caption("STEP 1 OF 2 · BEFORE PLANNING")
st.title("Data and planning settings")
st.write("Load the four CSV files, fix anything rejected, then set how cost and carbon are weighed.")

settings()  # seed default weight and policy before the widgets read them
TABLES = {"orders": "rows loaded", "plants": "rows", "transport_duties": "lanes", "capacity_events": "events"}
status = api_get("/data/status") or {"datasets": {}, "rejects": [], "checks": []}

st.subheader("Input files · POST /ingest")
cols = st.columns(4)
for col, (table, noun) in zip(cols, TABLES.items(), strict=False):
    with col, st.container(border=True):
        ds = status["datasets"].get(table)
        st.markdown(f"**`{table}.csv`**")
        if ds:
            label = "Validated" if not ds["rows_rejected"] else f"{ds['rows_rejected']} rejected"
            st.markdown(f":{'green' if not ds['rows_rejected'] else 'orange'}-background[{label}]")
            a, b = st.columns(2)
            a.metric(noun, ds["rows_loaded"])
            b.metric("rows rejected", ds["rows_rejected"])
        else:
            st.markdown(":gray-background[Not loaded]")
            if table == "capacity_events":
                st.caption("Arrives at the late data drop. Planning works without it; load it to re-plan.")
        up = st.file_uploader("Replace file" if ds else "Upload CSV", type=["csv"], key=f"up_{table}",
                              label_visibility="collapsed")
        if up is not None and st.session_state.get(f"done_{table}") != up.file_id:
            r = api_post("/ingest", files={"file": (f"{table}.csv", up.getvalue(), "text/csv")},
                         data={"meta": f'{{"table": "{table}"}}'})
            st.session_state[f"done_{table}"] = up.file_id
            if r and r.get("status") == "rejected":
                st.session_state["upload_error"] = f"**File rejected** — {r.get('error') or 'no valid rows'}"
            elif r and r.get("status") == "partial":
                st.session_state["upload_error"] = (f"**Partly loaded** — {r['rows_loaded']} rows loaded, "
                                                    f"{r['rows_rejected']} rejected. See Rejected rows for each reason.")
            st.rerun()

if msg := st.session_state.pop("upload_error", None):
    (st.error if msg.startswith("**File rejected") else st.warning)(msg)

left, right = st.columns(2)
with left, st.container(border=True):
    checks = status["checks"]
    passed = sum(c["passed"] for c in checks)
    st.markdown(f"**Validation checks** · {passed} of {len(checks)} passed")
    for c in checks:
        st.markdown(f"{'✅' if c['passed'] else '❌'} {c['check']}" + (f"  \n  :gray[{c['detail']}]" if c["detail"] else ""))
with right, st.container(border=True):
    st.markdown("**Rejected rows**")
    rej = pd.DataFrame(status["rejects"])
    if rej.empty:
        st.caption("No rejected rows.")
    else:
        st.dataframe(rej, hide_index=True, use_container_width=True, height=200)
        st.download_button("Download rejects.csv", rej.to_csv(index=False), "rejects.csv")
    st.caption("Rejected rows are left out of planning. Fix the source file and upload it again to include them.")

with st.container(border=True):
    st.markdown("**Planning settings**")
    a, b, c = st.columns([2, 2, 1])
    with a:
        # widgets write to plain session keys so the values survive page switches
        ss = st.session_state
        ss.cost_w = st.slider("Cost versus carbon weight (cost %)", 0, 100, value=ss.cost_w, step=5)
        st.caption(f"Cost **{ss.cost_w}%** · Carbon **{100 - ss.cost_w}%**. "
                   "Default 70 / 30. Weights apply to normalised cost and carbon, so they mean what they say.")
    with b:
        opts = ["never_late", "allow_late"]
        ss.policy = st.radio("Delivery policy", opts, index=opts.index(ss.policy),
                             format_func=lambda p: {"never_late": "Never late — orders with no on-time option are flagged, not hidden",
                                                    "allow_late": "Allow late with a penalty — late orders are counted and shown"}[p])
    with c:
        ss.start = st.date_input("Planning start", value=ss.get("start"), help="Defaults to config.yaml planning_start")
    ready = all(t in status["datasets"] and status["datasets"][t]["rows_loaded"] for t in
                ("orders", "plants", "transport_duties"))
    if st.button("Generate allocation", type="primary", disabled=not ready):
        settings()
        r = analyze("Allocate this week's orders.")
        if r:
            st.switch_page("screens/results.py")
    st.caption("Runs the solver through POST /analyze. Enabled once every required file passes validation."
               if ready else "Disabled until orders.csv, plants.csv and transport_duties.csv are loaded.")
