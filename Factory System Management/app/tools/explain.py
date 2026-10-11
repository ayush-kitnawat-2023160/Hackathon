"""explain_order: rank one order's plant options and name the deciding factor. Facts only; the Writer words it."""
from app.tools.planner import Ctx


def explain_order(ctx: Ctx, plan: dict, order_id: str) -> dict:
    orders = ctx.tables["orders"]
    row = orders[orders["order_id"] == order_id]
    if row.empty:
        return {"order_id": order_id, "found": False}
    o = row.iloc[0]
    chosen = next((a for a in plan["allocation"] if a["order_id"] == order_id), None)
    opts = ctx.options[ctx.options["order_id"] == order_id]
    ranked = []
    for plant in sorted(set(ctx.tables["plants"]["plant"])):
        po = opts[opts["plant"] == plant]
        if po.empty:
            ranked.append({"plant": plant, "eligible": False, "reason": "Cannot make product"})
            continue
        best = po.sort_values(["days_late", "landed_cost"]).iloc[0]  # best delivery this plant can offer
        ranked.append({"plant": plant, "eligible": True, "cost": round(float(best["landed_cost"])),
                       "carbon_kg": round(float(best["carbon_kg"])), "arrival": best["arrival"].isoformat(),
                       "days_late": int(best["days_late"]),
                       "chosen": bool(chosen and chosen["plant"] == plant)})
    elig = [r for r in ranked if r["eligible"]]
    cheapest = min(elig, key=lambda r: r["cost"], default=None)
    reason = "not placed"
    if chosen:
        c = next(r for r in elig if r["chosen"])
        if cheapest and cheapest["plant"] == c["plant"]:
            reason = "cheapest eligible plant"
        elif cheapest and cheapest["days_late"] > 0 and c["days_late"] == 0:
            reason = (f"Plant {cheapest['plant']} is cheaper by {c['cost'] - cheapest['cost']} "
                      f"but arrives {cheapest['days_late']} days late")
        elif c["carbon_kg"] < (cheapest or c)["carbon_kg"]:
            reason = (f"carbon weight: {cheapest['carbon_kg'] - c['carbon_kg']} kg less carbon "
                      f"for {c['cost'] - cheapest['cost']} more cost")
        else:
            reason = f"capacity: the cheaper plant {cheapest['plant']} was full in the weeks that meet the due date"
    return {"order_id": order_id, "found": True, "product": o["product"], "qty": int(o["qty"]),
            "destination": o["destination"], "due": o["due"].isoformat(),
            "chosen_plant": chosen["plant"] if chosen else None, "options": ranked, "binding_reason": reason}
