import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from common import analyze, number_check_badge, status_banner

resp = st.session_state.get("plan")
if not resp:
    st.title("Plan results")
    with st.container(border=True):
        st.markdown("**No plan yet**  \nLoad and validate the four CSV files to generate the first allocation.")
        if st.button("Go to Data and settings"):
            st.switch_page("screens/data.py")
    st.stop()

w = resp["weights_used"]
st.caption(f"RUN {resp['run_id'].upper()} · WEIGHTS COST {round(w['cost'] * 100)} / CARBON {round(w['carbon'] * 100)}")
h1, h2, h3, h4 = st.columns([4, 1, 1.4, 1.4])
h1.title("Plan results")
alloc = pd.DataFrame(resp["allocation"])
if h2.button("View trace"):
    st.session_state["trace_run"] = resp["run_id"]
    st.switch_page("screens/trace.py")
h3.download_button("Download plan CSV", alloc.to_csv(index=False), f"plan_{resp['run_id']}.csv")
if h4.button("Test a disruption", type="primary"):
    st.switch_page("screens/replan.py")

status_banner(resp)
k = resp.get("kpis") or {}
if k:
    top = max(k["plant_share"], key=k["plant_share"].get)
    c = st.columns(4)
    c[0].metric("Total landed cost", f"{k['total_cost']:,}", help="production + energy + transport + duty")
    c[1].metric("Total carbon", f"{k['total_carbon_t']:,} t", help="plant energy + transport, CO₂e")
    c[2].metric("Late orders", k["late_orders"])
    c[3].metric(f"Plant {top} share of orders", f"{round(k['plant_share'][top] * 100)}%")

    with st.container(border=True):
        st.markdown("**Plant utilisation this horizon** · share of weekly capacity used")
        u = k["plant_utilisation"]
        fig = go.Figure(go.Bar(x=[v * 100 for v in u.values()], y=[f"Plant {p}" for p in u], orientation="h",
                               marker_color=["#b45309" if v >= 0.9 else "#1e3a5f" for v in u.values()],
                               text=[f"{round(v * 100)}%" for v in u.values()], textposition="outside"))
        fig.update_layout(height=170, margin=dict(l=0, r=30, t=5, b=5), xaxis=dict(range=[0, 110], title=None),
                          yaxis=dict(autorange="reversed"))
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Orange marks a plant at 90% or more: little room left if demand or capacity changes.")

if resp.get("unmet"):
    with st.container(border=True):
        st.markdown("**Orders that cannot be met** (closest option shown; not part of the plan)")
        st.dataframe(pd.DataFrame([{**{"order_id": u["order_id"], "reason": u["reason"]},
                                    **{f"closest_{k2}": v for k2, v in (u.get("closest_option") or {}).items()}}
                                   for u in resp["unmet"]]), hide_index=True, use_container_width=True)

left, right = st.columns([2, 1])
with left, st.container(border=True):
    st.markdown("**Allocation by order**")
    f1, f2, f3 = st.columns([2, 1, 1])
    q = f1.text_input("Search order or destination", label_visibility="collapsed", placeholder="Search order or destination")
    plant = f2.selectbox("Plant", ["All plants"] + sorted(alloc["plant"].unique()) if len(alloc) else ["All plants"],
                         label_visibility="collapsed")
    stat = f3.selectbox("Status", ["All statuses", "On time", "Late"], label_visibility="collapsed")
    view = alloc
    if len(view):
        if q:
            view = view[view["order_id"].str.contains(q, case=False) | view["destination"].str.contains(q, case=False)]
        if plant != "All plants":
            view = view[view["plant"] == plant]
        if stat != "All statuses":
            view = view[view["on_time"] == (stat == "On time")]
        view = view[["order_id", "destination", "plant", "week", "cost", "carbon_kg", "arrival", "due", "delivery"]]
    sel = st.dataframe(view, hide_index=True, use_container_width=True, height=380, on_select="rerun",
                       selection_mode="single-row")
    st.caption("Select a row to see why that plant was chosen")

with right, st.container(border=True):
    st.caption("ORDER EVIDENCE")
    rows = sel.selection.rows if sel else []
    if rows:
        r = view.iloc[rows[0]]
        key = f"why_{resp['run_id']}_{r['order_id']}"
        if key not in st.session_state:
            st.session_state[key] = analyze(f"Why was order {r['order_id']} built in Plant {r['plant']}?")
        ex = st.session_state[key]
        if ex and ex.get("explanation"):
            x = ex["explanation"]
            st.markdown(f"**Why {x['order_id']} went to Plant {x['chosen_plant']}**")
            st.dataframe(pd.DataFrame([{"Plant": o["plant"] + (" · chosen" if o.get("chosen") else ""),
                                        "Cost": o.get("cost", "—"),
                                        "Delivery": ("Cannot make product" if not o["eligible"] else
                                                     "On time" if o["days_late"] == 0 else f"{o['days_late']} days late")}
                                       for o in x["options"]]), hide_index=True, use_container_width=True)
            st.write(ex["summary"])
            number_check_badge(ex)
    else:
        st.caption("Select an order in the table.")
