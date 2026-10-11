"""A minimal judge demo UI; all data access happens through FastAPI."""

import os

import httpx
import plotly.graph_objects as go
import streamlit as st

API = os.getenv("API_URL", "http://localhost:8000").rstrip("/")
TABLES = ("orders", "plants", "transport_duties", "capacity_events")

st.set_page_config(page_title="TriPlan", layout="wide")
st.title("TriPlan | Cost, Carbon & Delivery")
st.caption("An auditable solver-backed allocation and what-if system. Numbers come from the optimizer, not the LLM.")


def get(path):
    with httpx.Client(timeout=15) as client:
        response = client.get(API + path)
        response.raise_for_status()
        return response.json()


def post(path, **kwargs):
    with httpx.Client(timeout=120) as client:
        response = client.post(API + path, **kwargs)
        response.raise_for_status()
        return response.json()


def show_result(result):
    st.subheader("Verified result")
    st.write(result.get("summary", ""))
    st.caption(f"Answer type: {result.get('answer_type')} | Run: {result.get('run_id')}")
    k = result.get("kpis") or {}
    if k:
        cols = st.columns(4)
        cols[0].metric("Landed cost", f"{k.get('total_cost', 0):,.2f}")
        cols[1].metric("Carbon (tonnes)", f"{k.get('total_carbon_t', 0):,.3f}")
        cols[2].metric("Late orders", k.get("late_orders", 0))
        cols[3].metric("Unallocated", k.get("unallocated_orders", 0))
    comparison = result.get("comparison")
    if comparison:
        st.write("**Changes against baseline**", comparison)
    frontier = result.get("tradeoff") or []
    if frontier:
        fig = go.Figure()
        for point in frontier:
            label = point["label"]
            fig.add_trace(go.Scatter(x=[point["cost"]],
                y=[point["carbon_t"]], mode="markers+text", text=[label],
                textposition="top center", marker_size=16,
                hovertemplate=(f"{label}<br>Cost: %{{x:,.2f}}<br>Carbon: %{{y:.3f}} t"
                               f"<br>Late orders: {point['late_orders']}"
                               f"<br>Unallocated: {point['unallocated_orders']}<extra></extra>")))
        fig.update_layout(title="Cost–carbon–delivery comparison (late orders on hover)",
                          xaxis_title="Landed cost", yaxis_title="Carbon (tonnes)")
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(frontier, hide_index=True, use_container_width=True)
    if result.get("allocation"):
        st.dataframe(result["allocation"], use_container_width=True, hide_index=True)
    if result.get("validator"):
        st.write("**Validator**", "PASS" if result["validator"].get("valid") else "NOT VALID")
    with st.expander("Assumptions, limitations and evidence"):
        st.json({"assumptions": result.get("assumptions"),
                 "limitations": result.get("limitations"), "evidence": result.get("evidence")})


try:
    health = get("/health")
    st.caption(f"API: {health['status']} · Data ready: {health['data_ready']} · LLM: {health['llm']['status']}")
except Exception as e:
    st.error(f"Cannot reach FastAPI at {API}: {e}")
    st.stop()

tab_data, tab_run, tab_scenarios, tab_trace = st.tabs(["Data", "Allocate & Ask", "Re-plan", "Trace"])
with tab_data:
    st.write("Upload the Annex B CSV files. A new table replaces its earlier version; older runs remain traceable.")
    for name in TABLES:
        file = st.file_uploader(f"{name}.csv", type="csv", key=name)
        if file and st.button(f"Ingest {name}", key="upload-" + name):
            try:
                resp = post("/ingest", data={"table": name},
                            files={"file": (file.name, file.getvalue(), "text/csv")},
                            headers={"X-Admin-Token": os.getenv("ADMIN_TOKEN", "")})
                (st.success if resp["status"] == "loaded" else st.error)(str(resp))
            except Exception as e:
                st.error(str(e))

with tab_run:
    request = st.text_area("Ask a question", value="Allocate this week's orders with a balance of cost and carbon.")
    cost_weight = st.slider("Cost priority (%)", 0, 100, 70)
    if st.button("Run allocation / answer"):
        try:
            with st.spinner("Validating and solving..."):
                st.session_state["result"] = post("/analyze", json={"request": request,
                    "weights": {"cost": cost_weight, "carbon": 100 - cost_weight}})
        except Exception as e:
            st.error(str(e))
    if "result" in st.session_state:
        show_result(st.session_state["result"])

with tab_scenarios:
    plant = st.text_input("Plant", "B")
    loss = st.slider("Capacity reduction (%)", 0, 100, 30)
    if st.button("Re-plan"):
        try:
            with st.spinner("Computing baseline and updated plan..."):
                st.session_state["scenario_result"] = post("/analyze", json={
                    "request": f"Plant {plant} loses {loss}% capacity. Re-plan."})
        except Exception as e:
            st.error(str(e))
    if "scenario_result" in st.session_state:
        show_result(st.session_state["scenario_result"])

with tab_trace:
    run_id = st.text_input("Inspect run ID", value=st.session_state.get("result", {}).get("run_id", ""))
    if st.button("Load trace") and run_id:
        try:
            st.json(get("/trace/" + run_id))
        except Exception as e:
            st.error(str(e))
