import streamlit as st
from common import analyze, answer_badge, assumptions_box, number_check_badge, settings

st.caption("POST /ANALYZE · THE SOLVER ANSWERS, THE LLM EXPLAINS")
st.title("Ask the planner")

EXAMPLES = ["Allocate this week's orders", "What does a 10% carbon cut cost?", "Cheapest plan that is on time",
            "Plant B loses 30% capacity. Re-plan.", "Why was order 77 built in Plant C?",
            "Write the brief for the leadership team"]
cols = st.columns(3)
for i, ex in enumerate(EXAMPLES):
    if cols[i % 3].button(ex, key=f"ex{i}", use_container_width=True):
        st.session_state["pending"] = ex

chat = st.session_state.setdefault("chat", [])
for n, (q, r) in enumerate(chat):
    with st.chat_message("user"):
        st.write(q)
    with st.chat_message("assistant"):
        valid = (r.get("validator") or {}).get("valid")
        st.markdown(f"{answer_badge(r['answer_type'])} "
                    + (":green-background[Plan valid]" if valid else ":red-background[Plan rejected]" if valid is False else "")
                    + f"  `run {r['run_id']}`")
        if r.get("brief"):
            st.markdown(r["brief"])
        else:
            st.write(r["summary"])
        c = r.get("clarification") or {}
        if r["answer_type"] == "clarification_needed" and c.get("options"):
            oc = st.columns(len(c["options"]))
            for j, opt in enumerate(c["options"]):
                if oc[j].button(opt, key=f"opt{n}{j}"):
                    st.session_state["pending"] = (f"What does a {opt} carbon cut cost?" if opt.endswith("%")
                                                   else "Show the cost and carbon trade-off curve" if "trade-off" in opt
                                                   else opt)
        if r["answer_type"] == "refused" and c.get("alternative"):
            if st.button(c["alternative"], key=f"alt{n}"):
                st.session_state["pending"] = "What is the cheapest plan that is on time?"
        b1, b2, _ = st.columns([1, 1, 4])
        if r["answer_type"] == "scenario" and r.get("comparison") and b1.button("Open comparison", key=f"cmp{n}"):
            st.session_state["replan_resp"] = r
            st.switch_page("screens/replan.py")
        if r["answer_type"] == "scenario" and r.get("tradeoff") and b1.button("Open trade-off", key=f"tro{n}"):
            st.session_state["tradeoff_resp"] = r
            st.switch_page("screens/tradeoff.py")
        if b2.button("View trace", key=f"tr{n}"):
            st.session_state["trace_run"] = r["run_id"]
            st.switch_page("screens/trace.py")
        number_check_badge(r)
        if r["answer_type"] not in ("refused", "clarification_needed"):
            assumptions_box(r)

w = settings()["weights"]
q = st.chat_input(f"Ask for a plan, a what-if, an explanation or a brief · weights cost {round(w['cost'] * 100)} / "
                  f"carbon {round(w['carbon'] * 100)}")
q = q or st.session_state.pop("pending", None)
if q:
    r = analyze(q)
    if r:
        chat.append((q, r))
        st.rerun()
