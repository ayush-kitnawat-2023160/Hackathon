"""Seeded generator for the synthetic data pack (Annex B schema plus two optional columns).

Usage: python data/generate.py [--seed 42] [--out data/sample]

No real company or personal data. Every edge case from guide section 4.4 is planted on purpose;
see data/DATA_CARD.md for where.
"""
import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

START = date(2026, 10, 12)  # must match app/config.yaml planning_start

PLANTS = {
    # plant: (cost multiplier, energy price EUR/kWh, base carbon kg/kWh, weekly capacity, products)
    "A": (0.72, 0.09, 0.70, 7000, ["P1", "P2", "P3", "P4"]),  # cheapest, dirtiest, far from Europe
    "B": (0.90, 0.15, 0.42, 5500, ["P1", "P2", "P3", "P5"]),  # middle; only plant for P5
    "C": (1.12, 0.21, 0.08, 3200, ["P1", "P2", "P4"]),  # greenest, small, close to Europe
}
PRODUCTS = {"P1": (20, 2.0), "P2": (34, 3.5), "P3": (48, 5.0), "P4": (15, 1.5), "P5": (75, 6.0)}
# Weekly carbon intensity overrides (edge case: carbon intensity changes by week)
WEEKLY_CARBON = {("A", 3): 0.86, ("C", 2): 0.16}

# destination: transit days, transport EUR/unit and transport kg/unit from each plant
LANES = {
    "Germany": {"A": (11, 3.8, 2.6), "B": (3, 1.4, 0.9), "C": (1, 0.8, 0.4)},
    "France": {"A": (12, 3.9, 2.7), "B": (4, 1.7, 1.1), "C": (2, 0.9, 0.5)},
    "UK": {"A": (13, 4.1, 2.9), "B": (5, 2.0, 1.3), "C": (3, 1.3, 0.8)},
    "Italy": {"A": (11, 3.6, 2.5), "B": (4, 1.6, 1.0), "C": (3, 1.2, 0.7)},
    "Spain": {"A": (12, 3.7, 2.6), "B": (5, 2.1, 1.4), "C": (3, 1.4, 0.8)},
    "Poland": {"A": (12, 3.9, 2.8), "B": (1, 0.6, 0.3), "C": (2, 1.0, 0.6)},
    "Netherlands": {"A": (11, 3.7, 2.5), "B": (3, 1.5, 1.0), "C": (1, 0.7, 0.4)},
    "Sweden": {"A": (14, 4.2, 3.0), "B": (4, 1.8, 1.2), "C": (3, 1.3, 0.8)},
    "USA": {"A": (15, 3.2, 3.4), "B": (10, 3.0, 3.1), "C": (9, 2.8, 3.0)},
    "India": {"A": (2, 0.5, 0.3), "B": (14, 3.6, 2.8), "C": (14, 3.8, 2.9)},
    "Japan": {"A": (6, 1.9, 1.2), "B": (16, 4.0, 3.3), "C": (16, 4.1, 3.3)},
    "Brazil": {"A": (18, 3.5, 3.6), "B": (15, 3.3, 3.4), "C": (14, 3.2, 3.3)},
}
# Import duty in percent; zero unless listed. Edge case: duty makes cheapest plant expensive for one region.
DUTY = {("A", "USA"): 25.0, ("A", "Germany"): 4.0, ("A", "France"): 4.0, ("A", "Italy"): 4.0,
        ("A", "Spain"): 4.0, ("A", "Netherlands"): 4.0, ("A", "Poland"): 4.0, ("A", "Sweden"): 4.0,
        ("A", "UK"): 6.0, ("B", "UK"): 2.0, ("C", "UK"): 2.0, ("B", "India"): 10.0,
        ("C", "India"): 10.0, ("B", "Brazil"): 12.0, ("C", "Brazil"): 12.0, ("A", "Brazil"): 12.0,
        ("B", "USA"): 3.0, ("C", "USA"): 3.0, ("B", "Japan"): 5.0, ("C", "Japan"): 5.0}


def write(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def generate(seed: int, out: Path, n_orders: int = 150) -> dict:
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)

    plants = []
    for p, (mult, price, carbon, cap, prods) in PLANTS.items():
        for prod in prods:
            base_cost, kwh = PRODUCTS[prod]
            row = {"plant": p, "product": prod, "weekly_capacity": cap,
                   "unit_cost": round(base_cost * mult, 2), "energy_kwh_per_unit": kwh,
                   "carbon_kg_per_kwh": carbon, "energy_price_per_kwh": price, "week_start": ""}
            plants.append(row)
            for (wp, wk), wc in WEEKLY_CARBON.items():
                if wp == p:
                    plants.append({**row, "carbon_kg_per_kwh": wc,
                                   "week_start": (START + timedelta(weeks=wk - 1)).isoformat()})

    transport = [{"plant": p, "destination": d, "transport_cost_per_unit": c, "transit_days": t,
                  "duty_pct": DUTY.get((p, d), 0.0), "carbon_kg_per_unit": k}
                 for d, lanes in LANES.items() for p, (t, c, k) in lanes.items()]

    europe = [d for d in LANES if d not in ("USA", "India", "Japan", "Brazil")]
    orders = []
    product_weights = {"P1": 30, "P2": 25, "P3": 18, "P4": 17, "P5": 10}
    for i in range(1, n_orders + 1):
        prod = rng.choices(list(product_weights), weights=product_weights.values())[0]
        dest = rng.choice(europe) if rng.random() < 0.7 else rng.choice(list(LANES))
        qty = rng.randrange(100, 601, 10)
        due = START + timedelta(days=rng.randint(14, 38))
        orders.append({"order_id": f"O-{i:03d}", "product": prod, "qty": qty,
                       "destination": dest, "due": due.isoformat()})

    # Planted traps (fixed ids so the data card and evaluation can point at them):
    # O-005, O-012, O-019: cheapest plant (A) arrives late; B/C arrive on time (due day 10-11, Europe)
    for oid, dest in (("O-005", "Germany"), ("O-012", "France"), ("O-019", "Netherlands")):
        o = next(o for o in orders if o["order_id"] == oid)
        o.update(product="P1", destination=dest, qty=400, due=(START + timedelta(days=11)).isoformat())
    # O-033, O-047: no on-time option in any plant (due before the earliest possible arrival)
    for oid, dest in (("O-033", "USA"), ("O-047", "Brazil")):
        o = next(o for o in orders if o["order_id"] == oid)
        o.update(product="P2", destination=dest, qty=300, due=(START + timedelta(days=12)).isoformat())
    # O-077: Product made by A and C only; A cheaper but late to Germany, C on time
    o = next(o for o in orders if o["order_id"] == "O-077")
    o.update(product="P4", destination="Germany", qty=500, due=(START + timedelta(days=13)).isoformat())
    # O-090: only Plant B can make P5 (single-plant order)
    o = next(o for o in orders if o["order_id"] == "O-090")
    o.update(product="P5", destination="Poland", qty=600, due=(START + timedelta(days=16)).isoformat())

    events = [{"event_id": "E-1", "plant": "B", "from_date": (START + timedelta(days=7)).isoformat(),
               "to_date": (START + timedelta(days=20)).isoformat(), "capacity_loss_pct": 30.0}]

    write(out / "orders.csv", orders)
    write(out / "plants.csv", plants)
    write(out / "transport_duties.csv", transport)
    write(out / "capacity_events.csv", events)
    return {"orders": len(orders), "plants": len(plants), "transport_duties": len(transport),
            "capacity_events": len(events)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=str(Path(__file__).parent / "sample"))
    ap.add_argument("--orders", type=int, default=150)
    a = ap.parse_args()
    print(generate(a.seed, Path(a.out), a.orders))
