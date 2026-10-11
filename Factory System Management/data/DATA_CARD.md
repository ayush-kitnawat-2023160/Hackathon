# Data card — synthetic sample pack

| Field | Value |
|---|---|
| Name and version | `data/sample`, v1.0, generated 2026-10-11 |
| How it was made | `python data/generate.py --seed 42` (plain Python `random`, no LLM). The plant, lane and duty parameters are fixed in the script; order product, destination, quantity and due date are drawn from the seed. Six orders are then overwritten on purpose (traps below). |
| What it contains | `orders.csv` 150 rows · `plants.csv` 18 rows (3 plants × their products, plus 3 weekly carbon-intensity override rows) · `transport_duties.csv` 36 rows (3 plants × 12 destinations) · `capacity_events.csv` 1 row. 12 destinations, 52,690 units in total. Due dates 2026-10-23 to 2026-11-19; planning starts 2026-10-12. |
| Schema | Annex B, plus two optional `plants.csv` columns: `energy_price_per_kwh` (EUR/kWh) and `week_start` (row overrides carbon intensity for that week). Units: capacity in units per week, carbon intensity in kg CO₂e per kWh, transport carbon in kg CO₂e per unit, currency EUR. |
| Checks run | `python -m app.data.load --dir data/sample`: 0 rejected rows, 6 of 6 validation checks passed. |
| Privacy | Entirely synthetic. No real company, customer or personal data. |

## Plants

| Plant | Character | Weekly capacity | Products | Carbon kg/kWh |
|---|---|---|---|---|
| A | Cheapest, dirtiest, far from Europe (11–14 days transit) | 7,000 | P1–P4 | 0.70 (0.86 in week 3) |
| B | Middle cost and carbon; **only plant for P5** | 5,500 | P1, P2, P3, P5 | 0.42 |
| C | Greenest, dearest, **smallest**, close to Europe | 3,200 | P1, P2, P4 | 0.08 (0.16 in week 2) |

## Traps and edge cases (guide section 4.4)

| Edge case | Where |
|---|---|
| Cheapest plan misses the delivery date | O-005 (Germany), O-012 (France), O-019 (Netherlands): Plant A is cheapest but arrives late; B or C is on time. Also O-077 (P4 to Germany): A cheaper by 1,296 but 4 days late, C on time. |
| An order only one plant can make | Every P5 order, e.g. O-090 (P5 → Poland), only Plant B. |
| Import duty makes the cheapest plant expensive for one region | Plant A → USA carries 25% duty (other A lanes 4–6%). |
| Low carbon but little capacity | Plant C (0.08 kg/kWh, 3,200 units/week). |
| Capacity loss that makes some orders impossible | Plant B losing 100% leaves P5 orders with no plant (eval case `replan-B-100-impossible`); Plant C −50% in weeks 1–2 leaves 2 orders unplaceable. |
| Carbon intensity that changes by week | `week_start` rows: Plant A week 3, Plant C week 2. |
| No on-time option in any plant | O-033 (USA) and O-047 (Brazil), due before the earliest possible arrival: flagged, not hidden. |

## Known limits

Transit times are fixed per lane, duty and energy prices are constant over the horizon, and orders are never split. The pack is sized for about 85% load at the default weights, so it does not represent peak season.
