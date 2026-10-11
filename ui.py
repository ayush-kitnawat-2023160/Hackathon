"""TriPlan operations console. All business actions go through the FastAPI API."""

import csv
import io
import json
import os
from collections import defaultdict
from datetime import date, timedelta

import httpx
import plotly.graph_objects as go
import pandas as pd
import streamlit as st
from st_aggrid import AgGrid, DataReturnMode, GridOptionsBuilder
from streamlit_echarts import st_echarts

API = os.getenv("API_URL", "http://localhost:8000").rstrip("/")
TABLES = (
    ("orders", "Customer orders", "Required · Products, quantities, destinations and due dates"),
    ("plants", "Plant capabilities", "Required · Eligibility, shared capacity and production costs"),
    ("transport_duties", "Transport and duties", "Required · Routes, lead times, landed costs and emissions"),
    ("capacity_events", "Capacity events", "Optional · Apply disruptions during re-planning"),
)
PALETTE = ["#365CCF", "#15A28A", "#E9A23B", "#8A68C5", "#DB6C64", "#468CA6"]

st.set_page_config(page_title="TriPlan | Operations Console", page_icon="T", layout="wide",
                   initial_sidebar_state="expanded")


def get(path):
    with httpx.Client(timeout=12) as client:
        response = client.get(API + path)
        response.raise_for_status()
        return response.json()


def post(path, **kwargs):
    with httpx.Client(timeout=180) as client:
        response = client.post(API + path, **kwargs)
        response.raise_for_status()
        return response.json()


def remember(result):
    """Retain the currently selected plan when a brief or explanation is requested."""
    if result.get("run_id"):
        st.session_state["run_history"] = [
            result, *[item for item in st.session_state["run_history"]
                      if item.get("run_id") != result["run_id"]]
        ][:25]
    st.session_state["last_response"] = result
    if result.get("allocation") or result.get("kpis"):
        st.session_state["current_plan"] = result


def csv_export(rows):
    if not rows:
        return ""
    output = io.StringIO()
    columns = list(rows[0])
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def figure_layout(fig, *, height=320, title=None):
    fig.update_layout(
        title=dict(text=title, font=dict(size=16)) if title else None,
        height=height, margin=dict(l=16, r=16, t=46 if title else 22, b=20),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Arial, sans-serif", size=12),
        legend=dict(orientation="h", y=-0.18),
    )
    fig.update_xaxes(showgrid=False, zeroline=False)
    fig.update_yaxes(gridcolor="rgba(110,128,153,0.13)", zeroline=False)
    return fig


def allocation_summary(rows):
    grouped = defaultdict(lambda: {"plant": "", "orders": 0, "qty": 0, "cost": 0.0, "carbon_kg": 0.0})
    for item in rows:
        entry = grouped[item["plant"]]
        entry["plant"] = item["plant"]
        entry["orders"] += 1
        entry["qty"] += item.get("qty", 0)
        entry["cost"] += item["cost"]
        entry["carbon_kg"] += item["carbon_kg"]
    return [grouped[k] for k in sorted(grouped)]


def status_message(result, *, compact=False):
    if not result:
        return
    answer = result.get("answer_type")
    validator = result.get("validator") or {}
    kpi = result.get("kpis") or {}
    if answer == "refused":
        st.error(result.get("summary", "Request refused"), icon=":material/gpp_bad:")
    elif answer == "clarification_needed":
        st.info(result.get("summary", "Please clarify the request"), icon=":material/info:")
    elif answer == "infeasible" or kpi.get("late_orders", 0) or kpi.get("unallocated_orders", 0):
        st.warning("Best effort / delivery exception — check unallocated and late orders before approval.", icon=":material/warning:")
    elif validator.get("valid"):
        st.success("Independently validated allocation · production capacity and eligibility checked.", icon=":material/check_circle:")
    if not compact and result.get("summary"):
        st.markdown(result["summary"])


def kpi_cards(result):
    k = result.get("kpis") or {}
    if not k:
        st.info("No computed KPIs for this response. Generate an allocation to populate the dashboard.")
        return
    baseline = result.get("baseline_kpis") or {}
    same_demand = (result.get("comparison") or {}).get("same_orders_served", True)
    cost_delta = k["total_cost"] - baseline["total_cost"] if baseline and same_demand else None
    carbon_delta = k["total_carbon_t"] - baseline["total_carbon_t"] if baseline and same_demand else None
    cols = st.columns(4, gap="medium")
    with cols[0]:
        st.metric("Total landed cost", f"{k['total_cost']:,.2f}",
                  f"{cost_delta:+,.2f} vs baseline" if cost_delta is not None else None,
                  delta_color="inverse", border=True, help="Synthetic currency units (not a real exchange rate)")
    with cols[1]:
        st.metric("Carbon emissions", f"{k['total_carbon_t']:,.3f} t",
                  f"{carbon_delta:+,.3f} t" if carbon_delta is not None else None,
                  delta_color="inverse", border=True)
    with cols[2]:
        st.metric("On-time assigned", f"{len(result.get('allocation', [])) - k.get('late_orders', 0):,}",
                  border=True, help="Allocated orders due on time; does not include unallocated orders")
    with cols[3]:
        st.metric("At-risk orders", k.get("late_orders", 0) + k.get("unallocated_orders", 0),
                  border=True, help="Sum of late and unallocated orders")


def plant_chart(rows):
    items = allocation_summary(rows)
    if not items:
        st.info("Plant assignment distribution appears after solving.")
        return
    fig = go.Figure(go.Bar(
        x=[x["plant"] for x in items], y=[x["orders"] for x in items],
        marker_color=PALETTE[:len(items)] if len(items) <= len(PALETTE) else PALETTE[0],
        text=[x["orders"] for x in items], textposition="outside",
        hovertemplate="Plant %{x}<br>%{y} orders<extra></extra>"))
    fig.update_yaxes(title_text="Orders assigned")
    st.plotly_chart(figure_layout(fig, title="Allocation by plant"), use_container_width=True)


def utilisation_chart(kpis):
    values = kpis.get("plant_utilisation", {})
    if not values:
        st.info("Plant utilisation appears after solving.")
        return
    plants = sorted({key.split(":W")[0] for key in values})
    weeks = sorted({int(key.split(":W")[1]) for key in values})
    fig = go.Figure()
    for i, plant in enumerate(plants):
        fig.add_trace(go.Bar(name=f"Plant {plant}", x=[f"W{week}" for week in weeks],
            y=[values.get(f"{plant}:W{week}", 0) * 100 for week in weeks],
            marker_color=PALETTE[i % len(PALETTE)],
            hovertemplate="%{fullData.name}<br>%{x}: %{y:.1f}% capacity<extra></extra>"))
    fig.update_layout(barmode="group")
    fig.update_yaxes(title_text="Utilisation (%)", range=[0, 108])
    st.plotly_chart(figure_layout(fig, title="Weekly capacity utilisation"), use_container_width=True)


def cost_carbon_chart(result):
    points = result.get("tradeoff") or []
    if len(points) < 2:
        st.info("Run a carbon reduction or capacity scenario to compare two computed plans.")
        return
    fig = go.Figure()
    for i, point in enumerate(points):
        fig.add_trace(go.Scatter(x=[point["cost"]], y=[point["carbon_t"]],
            mode="markers+text", text=[point["label"]], textposition="top center",
            marker=dict(color=PALETTE[i], size=16, line=dict(width=2, color="white")),
            hovertemplate=(f"{point['label']}<br>Cost: %{{x:,.2f}}"
                           f"<br>Carbon: %{{y:.3f}} t<br>Late: {point['late_orders']}"
                           f"<br>Unallocated: {point['unallocated_orders']}<extra></extra>")))
    fig.update_xaxes(title_text="Landed cost")
    fig.update_yaxes(title_text="Emissions (t CO₂e)")
    st.plotly_chart(figure_layout(fig, title="Cost vs carbon · computed plans"), use_container_width=True)
    st.caption("Comparison points, not a complete Pareto frontier. Check fulfillment before interpreting cost changes.")


def production_flow(rows, *, key):
    """ECharts Sankey: actual allocated units moving from plants to production weeks."""
    if not rows:
        st.info("Production flow becomes available after allocation.")
        return
    links = defaultdict(int)
    for row in rows:
        links[(str(row["plant"]), int(row["week"]))] += int(row.get("qty", 0))
    links = {pair: quantity for pair, quantity in links.items() if quantity > 0}
    if not links:
        st.info("There are no allocated production units to visualize.")
        return
    plants = sorted({plant for plant, _ in links})
    weeks = sorted({week for _, week in links})
    nodes = [{"name": f"Plant {plant}", "itemStyle": {"color": PALETTE[i % len(PALETTE)]}}
             for i, plant in enumerate(plants)]
    nodes.extend({"name": f"Week {week}", "itemStyle": {"color": "#91A5C1"}} for week in weeks)
    edges = [{"source": f"Plant {plant}", "target": f"Week {week}", "value": quantity}
             for (plant, week), quantity in sorted(links.items())]
    options = {
        "animationDuration": 450,
        "tooltip": {"trigger": "item", "triggerOn": "mousemove",
                    "formatter": "{b}: {c} units"},
        "series": [{"type": "sankey", "data": nodes, "links": edges,
                    "top": 18, "bottom": 22, "left": 12, "right": 18,
                    "nodeWidth": 13, "nodeGap": 15, "nodeAlign": "justify",
                    "lineStyle": {"color": "source", "opacity": 0.28, "curveness": 0.45},
                    "label": {"color": "#243B56", "fontSize": 12},
                    "emphasis": {"focus": "adjacency"}}],
    }
    st.subheader("Production flow")
    st.caption("Allocated units by plant and production week. Hover to inspect individual flows.")
    st_echarts(options=options, height="345px", key=f"production-flow-{key}")


def allocation_table(result, *, table_key):
    rows = result.get("allocation") or []
    if not rows:
        st.info("This response has no assigned orders to display.")
        return
    left, mid, right = st.columns([1.2, 1, 1.6])
    with left:
        plant = st.selectbox("Plant", ["All", *sorted({r["plant"] for r in rows})], key=f"{table_key}-plant")
    with mid:
        week = st.selectbox("Production week", ["All", *sorted({r["week"] for r in rows})], key=f"{table_key}-week")
    with right:
        query = st.text_input("Find an order", placeholder="Search order ID", key=f"{table_key}-search")
    filtered = [r for r in rows if (plant == "All" or r["plant"] == plant)
                and (week == "All" or r["week"] == week)
                and query.strip().lower() in r["order_id"].lower()]
    st.caption(f"Showing {len(filtered):,} of {len(rows):,} assigned orders. Filtered exports respect your selection.")
    if not filtered:
        st.info("No assignments match the selected filters.")
        return
    df = pd.DataFrame(filtered)
    preferred = ["order_id", "plant", "week", "qty", "arrival", "on_time", "cost", "carbon_kg", "lateness_days"]
    df = df[[name for name in preferred if name in df.columns] +
            [name for name in df.columns if name not in preferred]]
    builder = GridOptionsBuilder.from_dataframe(df)
    builder.configure_default_column(sortable=True, filter=True, resizable=True, editable=False, minWidth=105)
    builder.configure_column("order_id", header_name="Order ID", pinned="left", minWidth=150)
    builder.configure_column("cost", header_name="Landed cost", type=["numericColumn", "numberColumnFilter"])
    builder.configure_column("carbon_kg", header_name="Carbon (kg)", type=["numericColumn", "numberColumnFilter"])
    builder.configure_column("arrival", header_name="Expected arrival")
    builder.configure_selection(selection_mode="disabled")
    builder.configure_pagination(enabled=True, paginationAutoPageSize=False, paginationPageSize=20)
    builder.configure_grid_options(domLayout="normal", suppressRowClickSelection=True)
    grid = AgGrid(df, gridOptions=builder.build(), key=f"allocation-grid-{table_key}-{result.get('run_id')}",
                  height=440, theme="streamlit", enable_enterprise_modules=False,
                  show_search=True, show_download_button=False,
                  data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
                  update_on=["filterChanged", "sortChanged"])
    grid_data = grid.get("data") if grid is not None else None
    # The component returns a server-side data frame after browser filters/sorts.
    # Until the component initializes, export the Python-filtered rows instead.
    export_rows = grid_data.to_dict("records") if isinstance(grid_data, pd.DataFrame) else filtered
    st.download_button("Export visible allocation", csv_export(export_rows),
                       file_name=f"triplan-{result.get('run_id', 'plan')}-allocation.csv",
                       mime="text/csv", disabled=not export_rows, icon=":material/download:",
                       key=f"{table_key}-download", on_click="ignore")


def run_action(request, *, weights=None, scenario=None):
    payload = {"request": request}
    if weights is not None:
        payload["weights"] = weights
    if scenario:
        payload["scenario"] = scenario
    with st.status("Calculating with the allocation engine...", expanded=False) as progress:
        progress.write("Submitting validated request to FastAPI")
        result = post("/analyze", json=payload)
        progress.write("Solver result and independent checks received")
        progress.update(label="Analysis complete", state="complete")
    remember(result)
    return result


for key, initial in (("run_history", []), ("chat_history", []), ("current_plan", None),
                     ("last_response", None), ("latest_trace", None)):
    if key not in st.session_state:
        st.session_state[key] = initial

try:
    health = get("/health")
except (httpx.HTTPError, ValueError) as exc:
    st.error(f"FastAPI is unreachable. Check Docker Compose and API_URL. Details: {exc}")
    st.stop()

ready = health.get("data_ready", False)
loaded = set(health.get("tables_loaded", []))
versions = health.get("input_versions", {})
current = st.session_state["current_plan"]
stale = bool(current and current.get("input_versions") != versions)

with st.sidebar:
    st.markdown("## TriPlan")
    st.caption("OPERATIONS INTELLIGENCE")
    st.divider()
    navigation = st.radio("Workspace", ["Overview", "Data sources", "Allocation studio",
                                         "Scenario lab", "AI analyst", "Run audit"],
                          captions=["KPIs & plant performance", "CSV onboarding and checks",
                                    "Compute and inspect plans", "Disruptions and carbon cuts",
                                    "Ask about verified decisions", "Trace and export runs"])
    st.divider()
    st.subheader("System status")
    st.success("API connected", icon=":material/check_circle:")
    (st.success if ready else st.warning)("Dataset ready" if ready else "Upload required tables")
    llm_status = health.get("llm", {}).get("status", "unknown")
    if llm_status == "mock":
        st.info("Deterministic AI fallback", icon=":material/info:")
    else:
        st.caption(f"Language model: {llm_status}")
    st.progress(len(loaded.intersection({"orders", "plants", "transport_duties"})) / 3,
                text=f"{len(loaded.intersection({'orders', 'plants', 'transport_duties'}))}/3 core datasets")
    if stale:
        st.warning("Saved view uses an older input version")
    with st.expander("System details"):
        st.caption("Input versions are tracked using SHA-256 checksums.")
        for name, checksum in sorted(versions.items()):
            st.text(f"{name}: {checksum[:10]}")

st.caption("SUPPLY CHAIN DECISION SUPPORT / MULTI-PLANT ALLOCATION")

if navigation == "Overview":
    st.title("Operations overview")
    st.write("One view of production allocation, emissions, delivery risk, and plant utilisation.")
    if current:
        status_message(current, compact=True)
        if stale:
            st.warning("These results were computed from a previous dataset snapshot. Re-run before using them for decisions.")
        kpi_cards(current)
        left, right = st.columns(2, gap="large")
        with left:
            with st.container(border=True):
                plant_chart(current.get("allocation", []))
        with right:
            with st.container(border=True):
                utilisation_chart(current.get("kpis") or {})
        if current.get("allocation"):
            with st.container(border=True):
                production_flow(current["allocation"], key="overview")
        if current.get("tradeoff"):
            with st.container(border=True):
                cost_carbon_chart(current)
        st.subheader("Latest decision")
        st.caption(f"Run {current.get('run_id')} · {current.get('answer_type')} · Solver {current.get('solver_status', 'n/a')}")
        st.markdown(current.get("summary", ""))
    else:
        st.info("Your console is ready. Start in **Allocation studio** to create a verified plan.")
        cols = st.columns(4)
        for col, label in zip(cols, ("Landed cost", "Carbon emissions", "On-time assigned", "At-risk orders")):
            with col:
                st.metric(label, "—", border=True)
        st.subheader("Getting started")
        with st.container(border=True):
            st.markdown("**1. Load** orders, plant details, and transport routes in Data sources.")
            st.markdown("**2. Optimize** against capacity, eligibility and delivery deadlines.")
            st.markdown("**3. Explore** capacity loss and carbon-reduction scenarios.")
            st.markdown("**4. Explain** decisions using the verified plan and execution trace.")

elif navigation == "Data sources":
    st.title("Data sources")
    st.write("Upload the official CSV tables. Only valid files are accepted; historical results retain their input checksums.")
    cols = st.columns(4)
    for col, (name, title, _) in zip(cols, TABLES):
        with col:
            st.metric(title, "Loaded" if name in loaded else "Missing",
                      border=True, help=f"{name}.csv")
    st.divider()
    with st.container(border=True):
        st.subheader("Upload and validate")
        for name, title, description in TABLES:
            with st.expander(f"{title} — {'Loaded' if name in loaded else 'Not loaded'}", expanded=name not in loaded):
                st.caption(description)
                file = st.file_uploader(f"{name}.csv", type="csv", key=f"file-{name}")
                if file:
                    st.caption(f"Selected: {file.name} · {file.size:,} bytes")
                if st.button(f"Validate and ingest {name}", key=f"ingest-{name}",
                             disabled=file is None, type="primary" if name not in loaded else "secondary"):
                    try:
                        with st.status("Validating uploaded CSV...", expanded=False) as progress:
                            response = post("/ingest", data={"table": name},
                                files={"file": (file.name, file.getvalue(), "text/csv")},
                                headers={"X-Admin-Token": os.getenv("ADMIN_TOKEN", "")})
                            progress.update(label="Validation finished", state="complete")
                        if response.get("status") == "loaded":
                            st.success(f"{response['rows_loaded']:,} rows accepted · version {response['checksum'][:12]}")
                            st.toast(f"{title} updated", icon=":material/check_circle:")
                            st.rerun()
                        else:
                            st.error(response.get("error", "Upload rejected"))
                    except (httpx.HTTPError, ValueError) as exc:
                        st.error(f"Upload failed: {exc}")
    st.info("Adding a new plant requires updated plant capability **and** transport-route data. "
            "Re-run allocation after all related tables have been updated.")
    if not ready:
        st.warning("Allocation will be available once all three required tables are loaded.")

elif navigation == "Allocation studio":
    st.title("Allocation studio")
    st.write("Configure priorities, solve a constrained allocation, and inspect individual assignments.")
    with st.container(border=True):
        with st.form("allocation-form"):
            st.subheader("Optimization settings")
            objective = st.radio("Optimization target", ["Balanced", "Lowest cost", "Lowest carbon"],
                                 index=0, horizontal=True, key="allocation-objective")
            cost_weight = st.slider("Cost priority within the balanced objective", 0, 100, 70,
                                    disabled=objective != "Balanced", help="Carbon gets the remaining percentage")
            st.caption("Delivery defaults to strict on-time. If no strict plan exists, the solver explicitly labels the fallback.")
            submitted = st.form_submit_button("Generate allocation", type="primary", icon=":material/auto_awesome:",
                                               disabled=not ready, use_container_width=True)
        if submitted:
            question = {"Lowest cost": "What is the cheapest on-time allocation?",
                        "Lowest carbon": "Minimize carbon for all orders.",
                        "Balanced": "Allocate orders with the requested cost and carbon priorities."}[objective]
            weights = ({"cost": cost_weight, "carbon": 100 - cost_weight}
                       if objective == "Balanced" else None)
            try:
                run_action(question, weights=weights)
                st.toast("Allocation ready for review", icon=":material/check_circle:")
            except (httpx.HTTPError, ValueError) as exc:
                st.error(f"Could not complete allocation: {exc}")
    result = st.session_state["current_plan"]
    if result:
        if stale:
            st.warning("This plan uses an older dataset version. Generate a new plan before approving allocations.")
        st.subheader("Computed plan")
        status_message(result)
        kpi_cards(result)
        with st.container(border=True):
            st.subheader("Order assignments")
            allocation_table(result, table_key="studio")
        if result.get("allocation"):
            a, b = st.columns(2)
            with a:
                with st.container(border=True):
                    plant_chart(result["allocation"])
            with b:
                with st.container(border=True):
                    utilisation_chart(result.get("kpis") or {})
            with st.container(border=True):
                production_flow(result["allocation"], key="studio")
        with st.expander("Solver validation, assumptions and limitations"):
            st.json({"solver_status": result.get("solver_status"),
                     "validator": result.get("validator"),
                     "assumptions": result.get("assumptions", []),
                     "limitations": result.get("limitations", []),
                     "input_versions": result.get("input_versions", {})})
    elif ready:
        st.info("Choose priorities and generate your first allocation.")

elif navigation == "Scenario lab":
    st.title("Scenario lab")
    st.write("Quantify disruption and sustainability trade-offs against a fresh solver-computed baseline.")
    st.caption("The original dataset and baseline are not overwritten by scenario requests.")
    with st.container(border=True):
        scenario_type = st.radio("Scenario", ["Capacity loss", "Carbon reduction", "Uploaded capacity events"],
                                 index=0, horizontal=True, key="scenario-type")
        with st.form("scenario-form"):
            scenario_data = None
            if scenario_type == "Capacity loss":
                left, right = st.columns(2)
                with left:
                    plant = st.text_input("Affected plant ID", value="B", help="Use a plant ID present in your CSV")
                with right:
                    loss = st.slider("Capacity loss (%)", 0, 100, 30)
                planning_start = date.fromisoformat(health.get("planning_start", "2026-10-12"))
                begin, finish = st.columns(2)
                with begin:
                    from_date = st.date_input("Disruption begins", value=planning_start)
                with finish:
                    to_date = st.date_input("Disruption ends (inclusive)",
                                             value=planning_start + timedelta(days=13))
                invalid_dates = from_date > to_date
                if invalid_dates:
                    st.warning("End date must be on or after the start date.")
                scenario_data = {"from_date": from_date.isoformat(), "to_date": to_date.isoformat()}
                request = f"Plant {plant.strip()} loses {loss}% capacity. Re-plan."
            elif scenario_type == "Carbon reduction":
                reduction = st.slider("Target carbon reduction (%)", 1, 80, 10)
                st.caption("The backend first calculates a cheapest on-time baseline, then solves under a carbon cap.")
                request = f"What does a {reduction}% carbon cut cost?"
            else:
                st.caption("Re-plan using the already-ingested capacity_events.csv. Upload the events file in Data sources first.")
                if "capacity_events" not in loaded:
                    st.warning("No capacity-events file has been uploaded yet.")
                request = "Re-plan using uploaded capacity events."
            submit = st.form_submit_button("Run scenario", type="primary", icon=":material/compare_arrows:",
                                           disabled=not ready or (scenario_type == "Capacity loss" and invalid_dates)
                                           or (scenario_type == "Uploaded capacity events" and "capacity_events" not in loaded),
                                           use_container_width=True)
        if submit:
            try:
                st.session_state["scenario_result"] = run_action(request, scenario=scenario_data)
                st.toast("Scenario comparison ready", icon=":material/check_circle:")
            except (httpx.HTTPError, ValueError) as exc:
                st.error(f"Scenario failed: {exc}")
    result = st.session_state.get("scenario_result")
    if result:
        status_message(result)
        kpi_cards(result)
        cmp = result.get("comparison") or {}
        if cmp:
            st.subheader("Changes from baseline")
            if cmp.get("comparison_warning"):
                st.warning(cmp["comparison_warning"])
            cols = st.columns(4)
            with cols[0]:
                st.metric("Orders reassigned", cmp.get("orders_moved", 0), border=True)
            with cols[1]:
                st.metric("Cost change", f"{cmp.get('cost_delta', 0):+,.2f}", border=True)
            with cols[2]:
                st.metric("Emissions change", f"{cmp.get('carbon_delta_kg', 0):+,.2f} kg", border=True)
            with cols[3]:
                st.metric("Unallocated change", f"{cmp.get('unallocated_orders_delta', 0):+d}", border=True)
        with st.container(border=True):
            cost_carbon_chart(result)
        if result.get("tradeoff"):
            st.dataframe(result["tradeoff"], use_container_width=True, hide_index=True)
        if result.get("allocation"):
            with st.expander("Inspect scenario assignments"):
                allocation_table(result, table_key="scenario")
        with st.expander("Raw scenario evidence"):
            st.json({"comparison": cmp, "baseline_kpis": result.get("baseline_kpis"),
                     "parameters_used": result.get("parameters_used"), "validator": result.get("validator")})
    elif ready:
        st.info("Select a scenario to compare its operational impact.")

elif navigation == "AI analyst":
    st.title("AI analyst")
    st.write("Ask natural-language questions. The model interprets or explains; the solver calculates assignments and numbers.")
    if health.get("llm", {}).get("status") == "mock":
        st.info("Deterministic fallback is active. Connect an LLM in .env to enable model-written explanations.")
    st.caption("SUGGESTED QUESTIONS")
    prompts = (("Explain an order", "Why was order O-LATE-001 assigned there?"),
               ("Carbon trade-off", "What does a 10% carbon cut cost?"),
               ("Leadership brief", "Write the management brief"))
    for column, (label, prompt) in zip(st.columns(3), prompts):
        with column:
            if st.button(label, use_container_width=True, key="prompt-" + label, disabled=not ready):
                st.session_state["pending_question"] = prompt
    for turn in st.session_state["chat_history"]:
        with st.chat_message(turn["role"]):
            st.markdown(turn["text"])
            if turn.get("run_id"):
                st.caption(f"Evidence: {turn['run_id']}")
    question = st.chat_input("Ask about allocations, trade-offs, orders or disruptions...", disabled=not ready)
    question = question or st.session_state.pop("pending_question", None)
    if question:
        st.session_state["chat_history"].append({"role": "user", "text": question})
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            try:
                response = run_action(question)
                st.markdown(response.get("summary", "No response"))
                st.caption(f"Evidence: {response.get('run_id')} · {response.get('answer_type')}")
                st.session_state["chat_history"].append({"role": "assistant",
                    "text": response.get("summary", ""), "run_id": response.get("run_id")})
            except (httpx.HTTPError, ValueError) as exc:
                st.error(f"Analysis failed: {exc}")
    if not ready:
        st.warning("Upload the required data sources before asking allocation questions.")

elif navigation == "Run audit":
    st.title("Run audit")
    st.write("Inspect the reasoning trail, input fingerprints, solver result and verification for any run ID.")
    history = st.session_state["run_history"]
    if history:
        rows = [{"Run": x["run_id"], "Type": x.get("answer_type"),
                 "Orders": len(x.get("allocation", [])),
                 "Validated": (x.get("validator") or {}).get("valid"),
                 "LLM calls": x.get("llm_calls", 0)} for x in history]
        st.dataframe(rows, hide_index=True, use_container_width=True)
    else:
        st.info("No runs recorded in this browser session. You can still look up any saved run ID below.")
    with st.container(border=True):
        st.subheader("Trace lookup")
        selected = st.selectbox("Recent run", ["Enter ID manually", *[x["run_id"] for x in history]],
                                key="trace-selected")
        manual = st.text_input("Run ID", value="" if selected == "Enter ID manually" else selected,
                               key="trace-manual")
        if st.button("Load execution trace", type="primary", disabled=not manual.strip()):
            try:
                st.session_state["latest_trace"] = get("/trace/" + manual.strip())
            except (httpx.HTTPError, ValueError) as exc:
                st.error(f"Trace lookup failed: {exc}")
        trace = st.session_state["latest_trace"]
        if trace:
            st.caption(f"Run {trace['run_id']} · {trace.get('answer_type')} · {trace.get('created_at')}")
            st.metric("Recorded workflow steps", len(trace.get("steps", [])), border=True)
            for i, step in enumerate(trace.get("steps", []), 1):
                with st.expander(f"{i}. {step.get('agent', 'tool')} · {step.get('tool') or 'decision'}"):
                    st.json(step)
            with st.expander("Input versions and full verified result"):
                st.json({"input_versions": trace.get("input_versions"),
                         "llm_calls": trace.get("llm_calls"), "result": trace.get("result")})
            st.download_button("Download complete trace", json.dumps(trace, indent=2),
                file_name=f"{trace['run_id']}-trace.json", mime="application/json",
                on_click="ignore", icon=":material/download:")

st.divider()
st.caption("TriPlan · Mathematical solver → independent validator → grounded explanation. "
           "All values represent model-computed scenarios under documented assumptions.")
