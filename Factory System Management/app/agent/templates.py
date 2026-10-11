"""Deterministic wording built straight from tool output. Used with MOCK_LLM, when the model is down,
and when the number guard rejects the model's text twice."""


def _n(v) -> str:
    return f"{v:,.0f}" if isinstance(v, (int, float)) and abs(v) >= 1000 else f"{v:g}" if isinstance(v, float) else str(v)


def allocation_text(e: dict) -> str:
    k = e["kpis"]
    top = max(k["plant_share"], key=k["plant_share"].get)
    s = (f"Plan cost {_n(k['total_cost'])}, carbon {_n(k['total_carbon_t'])} t, {k['late_orders']} late orders. "
         f"Plant {top} takes {round(k['plant_share'][top] * 100)}% of orders.")
    if e.get("flagged_late"):
        s += f" {len(e['flagged_late'])} orders have no on-time option in any plant and are flagged: {', '.join(e['flagged_late'][:5])}."
    if k.get("infeasible_orders"):
        s += f" {k['infeasible_orders']} orders cannot be placed; each is listed with its closest option."
    return s


def replan_text(e: dict) -> str:
    c = e["comparison"]
    s = (f"Re-planned with the capacity loss applied. Cost changes by {c['extra_cost']:+,} "
         f"and carbon by {c['extra_carbon_t']:+g} t (total cost {_n(c['cost_before'])} before, {_n(c['cost_after'])} after). "
         f"{c['orders_moved']} orders moved plant; {c['pct_unchanged']}% of orders are unchanged. "
         f"Late orders: {c['late_orders_before']} before, {c['late_orders_after']} after.")
    if c["infeasible_after"]:
        s += (f" {c['infeasible_after']} orders cannot be met after the loss; cost and carbon changes above"
              f" cover only the orders placed in both plans.")
    return s


def tradeoff_text(e: dict) -> str:
    pts = [p for p in e["tradeoff"] if p["carbon_cut_pct"] > 0]
    parts = []
    for p in pts:
        if p["feasible"]:
            parts.append(f"a {p['carbon_cut_pct']:g}% carbon cut adds {_n(p['extra_cost'])} in cost "
                         f"with {p['late_orders']} late orders")
        else:
            parts.append(f"a {p['carbon_cut_pct']:g}% carbon cut is infeasible within capacity and due dates")
    return ("Against the current plan, " + "; ".join(parts) + ".") if parts else "No carbon cut was requested."


def explain_text(e: dict) -> str:
    x = e["explanation"]
    if not x.get("found"):
        return f"Order {x['order_id']} is not in the loaded orders."
    lines = []
    for o in x["options"]:
        if not o["eligible"]:
            lines.append(f"Plant {o['plant']} cannot make this product.")
        else:
            when = "on time" if o["days_late"] == 0 else f"{o['days_late']} days late"
            lines.append(f"Plant {o['plant']} costs {_n(o['cost'])} and arrives {when}.")
    return (f"Order {x['order_id']} went to Plant {x['chosen_plant']}: {x['binding_reason']}. " + " ".join(lines))


def brief_text(e: dict) -> str:
    k = e["kpis"]
    top = max(k["plant_share"], key=k["plant_share"].get)
    out = [f"# This week's plan costs {_n(k['total_cost'])} and emits {_n(k['total_carbon_t'])} t of carbon, "
           f"with {k['late_orders']} orders unable to arrive on time", "",
           "**The plan**",
           f"Plant {top} takes {round(k['plant_share'][top] * 100)}% of orders and runs at "
           f"{round(k['plant_utilisation'][top] * 100)}% of capacity.", ""]
    ten = next((p for p in e.get("tradeoff") or [] if p["carbon_cut_pct"] > 0 and p["feasible"]), None)
    if ten:
        out += ["**Cost of cutting carbon**",
                f"A {ten['carbon_cut_pct']:g}% carbon cut adds {_n(ten['extra_cost'])} in cost.", ""]
    c = e.get("comparison")
    if c:
        out += ["**If capacity is lost**",
                f"The re-plan costs {_n(c['extra_cost'])} more and emits {c['extra_carbon_t']:g} t more carbon.", ""]
    out += ["**Decisions needed**", "- Approve the cost and carbon weighting, or choose a greener setting."]
    if c:
        out.append("- Approve the re-plan if the capacity loss is confirmed.")
    if k["late_orders"]:
        out.append("- Contact the customers of the late orders.")
    return "\n".join(out)
