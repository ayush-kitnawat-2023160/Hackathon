import json

import pandas as pd
import streamlit as st
from common import api_get

st.caption("GET /TRACE/{RUN_ID} · STORED LOCALLY IN SQLITE")
st.title("Runs and trace")

runs = api_get("/runs") or []
with st.container(border=True):
    st.markdown("**Run history**")
    if not runs:
        st.caption("No runs yet.")
        st.stop()
    df = pd.DataFrame(runs)
    df["plan_valid"] = df["plan_valid"].map({1: "Valid", 0: "Rejected"}).fillna("No plan")
    sel = st.dataframe(df, hide_index=True, use_container_width=True, on_select="rerun", selection_mode="single-row",
                       height=260)
    if sel.selection.rows:
        st.session_state["trace_run"] = df.iloc[sel.selection.rows[0]]["run_id"]

run_id = st.session_state.get("trace_run") or runs[0]["run_id"]
t = api_get(f"/trace/{run_id}")
if not t:
    st.stop()
with st.container(border=True):
    a, b = st.columns([4, 1])
    a.markdown(f"**Trace for `{run_id}`** · {t['request']}  \nEvery step, the agent that ran it, the tool it called and what came back")
    b.download_button("Download JSON", json.dumps(t, indent=2), f"trace_{run_id}.json")
    for s in t["steps"]:
        is_llm = s["agent"] in ("router", "writer") and s["tool"] in ("parse_intent", "narrate") and \
            (s.get("output") or {}).get("source") in ("llm", None)
        tag = ":orange-background[LLM]" if s["agent"] in ("router", "writer") and s["tool"] in ("parse_intent", "narrate") else ":gray-background[code]"
        st.markdown(f"{'🟠' if is_llm else '🟢'} **{s['agent']}** {tag} `{s['tool'] or ''}`"
                    + (f" · {s['ms']} ms" if s.get("ms") is not None else ""))
        detail = {k: v for k, v in (("input", s["input"]), ("output", s["output"])) if v}
        if detail:
            st.code(json.dumps(detail, default=str)[:1500], language="json")
    st.markdown(f"LLM calls **{t['llm_calls']}** · Tokens **{t['tokens']:,}** · Latency **{t['latency_ms']} ms**"
                + (f" · baseline run `{t['parent_run_id']}`" if t.get("parent_run_id") else ""))
    with st.expander("Data used (dataset versions, weights, policy, events)"):
        st.json(t["data_used"])
