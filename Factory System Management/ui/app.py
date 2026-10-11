"""Streamlit entry point: seven screens, each talking to the API only."""
import streamlit as st

st.set_page_config(page_title="Plant Allocator", layout="wide")

pages = [
    st.Page("screens/data.py", title="1 Data and settings", default=True),
    st.Page("screens/results.py", title="2 Plan results"),
    st.Page("screens/tradeoff.py", title="3 Trade-off"),
    st.Page("screens/replan.py", title="4 Re-plan"),
    st.Page("screens/ask.py", title="5 Ask the planner"),
    st.Page("screens/brief.py", title="6 Management brief"),
    st.Page("screens/trace.py", title="7 Runs and trace"),
]
with st.sidebar:
    st.markdown("### Plant Allocator\nCost, carbon and delivery planning")
nav = st.navigation(pages)
from common import sidebar_health  # noqa: E402

sidebar_health()
nav.run()
