import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from common import analyze, number_check_badge

st.caption("WHAT DOES CUTTING CARBON COST?")
st.title("Cost and carbon trade-off")
st.write("Each point is a full solver run with carbon capped below the current plan. Nothing here is estimated by the LLM.")

PRESETS = {"Cost only · 100 / 0": (1.0, 0.0), "Default · 70 / 30": (0.7, 0.3), "Greener · 40 / 60": (0.4, 0.6)}
with st.container(border=True):
    a, b = st.columns([2, 3])
    choice = a.radio("Weight setting", list(PRESETS), index=1, horizontal=True)
    b.caption("Why 70 / 30 by default: cost stays the main driver, and the default sits before the point where "
              "each extra percent of carbon cut gets sharply more expensive. Check that claim on the curve below.")
    if st.button("Compute trade-off", type="primary"):
        wc, wk = PRESETS[choice]
        r = analyze("Show the cost and carbon trade-off", scenario={"type": "tradeoff"},
                    weights={"cost": wc, "carbon": wk})
        if r:
            st.session_state["tradeoff_resp"] = r

r = st.session_state.get("tradeoff_resp")
if not r:
    st.info("Compute the trade-off to see the frontier (5 solver runs).")
    st.stop()
if r["answer_type"] == "infeasible" or not r.get("tradeoff"):
    st.error("The current plan is infeasible, so no trade-off can be computed. See Plan results.")
    st.stop()

pts = pd.DataFrame(r["tradeoff"])
ok = pts[pts["feasible"]]
ten = ok[ok["carbon_cut_pct"] == 10]
title = (f"A 10% carbon cut adds {int(ten.iloc[0]['extra_cost']):,} in cost and "
         f"{int(ten.iloc[0]['late_orders'] - ok.iloc[0]['late_orders'])} late orders") if len(ten) else "Cost against carbon"
with st.container(border=True):
    st.markdown(f"**{title}**")
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=ok["carbon_t"], y=ok["cost"], mode="lines+markers+text",
                             line=dict(dash="dash", color="#1e3a5f"),
                             marker=dict(size=14, color=ok["late_orders"], colorscale="Oranges", showscale=True,
                                         colorbar=dict(title="late orders"), line=dict(width=1, color="#333")),
                             text=[("Current plan" if c == 0 else f"{c:g}% cut") for c in ok["carbon_cut_pct"]],
                             textposition="top right",
                             hovertemplate="%{text}<br>cost %{y:,}<br>carbon %{x} t<extra></extra>"))
    fig.update_layout(height=420, xaxis_title="Total carbon (t CO₂e) — lower is greener",
                      yaxis_title="Total landed cost", margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(fig, use_container_width=True)
    if len(ten) and ten.iloc[0]["implied_price_per_t"]:
        t = ten.iloc[0]
        st.caption(f"Implied carbon price of a 10% cut: {int(t['extra_cost']):,} ÷ "
                   f"{round(ok.iloc[0]['carbon_t'] - t['carbon_t'], 1)} t ≈ {int(t['implied_price_per_t']):,} per tonne. "
                   "Compare it with a carbon price your leaders recognise.")
    st.write(r["summary"])
    number_check_badge(r)

with st.container(border=True):
    st.markdown("**Scenario table**")
    tbl = pts.assign(
        carbon_cut=pts["carbon_cut_pct"].map(lambda c: "0% · current" if c == 0 else f"{c:g}%"),
        total_cost=pts["cost"], extra_cost=pts["extra_cost"], carbon_t=pts["carbon_t"],
        result=pts["feasible"].map(lambda f: "valid plan" if f else "infeasible — never shown as a plan"),
    )[["carbon_cut", "total_cost", "extra_cost", "carbon_t", "late_orders", "implied_price_per_t", "result"]]
    st.dataframe(tbl, hide_index=True, use_container_width=True)
    st.download_button("Download CSV", tbl.to_csv(index=False), "tradeoff.csv")
    st.caption("A cut the solver cannot meet within capacity and due dates is marked infeasible, never shown as a plan.")
