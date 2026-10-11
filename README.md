# TriPlan — AI-assisted multi-plant order allocation

TriPlan now includes the **solver**, mandatory **FastAPI judge endpoints**, a single **LangGraph** workflow, a minimal **Streamlit** interface, **SQLite input/run history**, and Docker Compose.

The solver decides. The independent validator verifies. The LLM parses requests and explains verified outputs. No LLM generates assignments, rankings or computed totals.

**Primary application:** This repository root (`compose.yaml`, `ui.py`, `app/api.py`) is the supported TriPlan entry point. The upstream sync also included an independently developed prototype in [`Factory System Management/`](Factory%20System%20Management/README.md), with its own runtime, tests, and solver. It is retained as a comparison/reference implementation, **not** started by the root Docker Compose stack. Do not run both Compose stacks on ports 8000/8501 simultaneously.

**Allocator improvements integrated from upstream:** deterministic CBC solve settings, proof-aware statuses, dataset signatures, a shared time budget, lexicographic best-effort allocation, frozen order commitments, replanning diagnostics, and alternative/trade-off analysis tools. Run `python3 -m pytest tests -q` from the repo root; the reference app has its own test environment.

## Quick start (full app)

```bash
cp .env.example .env
docker compose up --build
```

- Dashboard: `http://localhost:8501`
- API docs: `http://localhost:8000/docs`
- Health: `http://localhost:8000/health`
- Upload `orders.csv`, `plants.csv`, and `transport_duties.csv` in the UI (capacity events can arrive later), then ask to allocate.

### Streamlit operations console

The dashboard uses a consistent light theme, responsive sidebar navigation, bordered KPI cards,
Plotly charts and searchable/downloadable tables. Navigate using the **Workspace** selector:

| Screen | Purpose |
| --- | --- |
| **Overview** | Cost, emissions, on-time and at-risk KPI cards; plant allocation and capacity utilisation charts |
| **Data sources** | Load the three required CSVs and optionally `capacity_events.csv`; inspect upload status and checksum versions |
| **Allocation studio** | Choose balanced, lowest-cost or lowest-carbon objectives; adjust weights; generate and filter allocations; export CSV |
| **Scenario lab** | Model a plant capacity loss over specified dates, assess a carbon reduction or use uploaded capacity events; compare baseline and scenario |
| **AI analyst** | Ask questions in natural language, or use suggested prompts; answers refer to recorded solver evidence |
| **Run audit** | Inspect current-session run history, retrieve any run by ID from SQLite-backed traces, and download trace JSON |

**Demo path:** Load the CSVs → Allocation studio (Lowest cost) → Overview → Scenario lab (Plant B, 30% loss) → AI analyst (leadership brief) → Run audit. The default synthetic pack can be generated with `python3 scripts/generate_data.py`. The dashboard will explicitly warn if a scenario leaves orders late or unallocated; in that case lower total cost is **not** evidence of savings.

The sidebar shows the configured LLM status. `MOCK_LLM=true` uses deterministic explanations for the offline demo; configure a real LLM provider when ready. Displayed KPIs and allocations always come from the solver and independent validator.

To run without Docker: install `requirements.txt`, then launch `uvicorn app.api:app --reload` and `streamlit run ui.py` in separate terminals. For this machine's system Python, isolated dependencies can be installed into `.app-deps` and accessed with `PYTHONPATH=.app-deps`.

The judge-compatible API is:

| Endpoint | Use |
| --- | --- |
| `POST /ingest` | Multipart `file` plus form `table=orders`, `plants`, `transport_duties`, or `capacity_events`; alternatively use form `metadata={"table":"orders"}` |
| `POST /analyze` | JSON with `request`, optional `weights` `{cost, carbon, lateness}`, and optional `scenario` |
| `GET /health` | API/data/LLM readiness |
| `GET /trace/{run_id}` | Steps, tool outputs, data checksums, and saved response |

Example:

```bash
curl -F 'table=orders' -F 'file=@data/synthetic/orders.csv' http://localhost:8000/ingest
curl -F 'table=plants' -F 'file=@data/synthetic/plants.csv' http://localhost:8000/ingest
curl -F 'table=transport_duties' -F 'file=@data/synthetic/transport_duties.csv' http://localhost:8000/ingest
curl -X POST http://localhost:8000/analyze -H 'Content-Type: application/json' \
  -d '{"request":"Allocate this week’s orders", "weights":{"cost":0.7,"carbon":0.3}}'
```

To generate reproducible practice data, use `python3 scripts/generate_data.py` as documented below. You can also stage files directly from the local CLI with `python3 -m app.bootstrap --data-dir data/synthetic`. In Docker, upload through the UI or copy the CSV directory into the API container and run the same bootstrap command.

`MOCK_LLM=true` (default) provides deterministic request routing and validated numerical templates without network access. For an actual LLM, set `MOCK_LLM=false`, `LLM_PROVIDER` (`ollama`, `openai`, `anthropic`, or `gemini`), and `LLM_MODEL`, plus its API key if necessary. Do not commit `.env`. Requests always execute the same constrained solver even when a model is used.

**Business assumptions:** The base planning Monday (`PLANNING_START`), number of production weeks (`PLANNING_WEEKS`) and fallback electricity price (`ENERGY_PRICE_DEFAULT`) are explicitly configurable. The official minimum `plants.csv` does not include electricity price. Our illustrative default value of `8` is *not* a verified real-world rate. Whole orders finish manufacturing on Sunday; weekly capacity is shared across products; landed-cost duty applies to production, energy and transport in this synthetic convention. Confirm these conventions with organizers before scoring official datasets. Each accepted CSV version is retained in SQLite; historical run traces record checksums.

**Local demo security:** Uploads enforce type, row, size, duplicate, date and numeric checks. Set `ADMIN_TOKEN` to require `X-Admin-Token` for ingestion in a shared environment. Default is intentionally unauthenticated for local judge scripts; do not expose this configuration to the public internet.

### Validation

```bash
python3 -m pytest -q
docker compose config --quiet
```

The allocator currently uses **PuLP/CBC**, not OR-Tools CP-SAT. Both are allowed in the official guide. The standalone allocator and generator documentation follows.

---

## Synthetic dataset generator

> **Standalone allocator now available:** see [ALLOCATOR_README.md](ALLOCATOR_README.md)
> for the PuLP solver, CLI, scenarios, independent validator, and JSON handoff.

Generate synthetic practice data for **Multi-Plant Order Allocation with Cost and Carbon**. Python 3.11+ is enough; no package installation, API key, or internet connection is needed.

The generator was checked against Sections 4.3–4.5 and Annexes B/D of the supplied participant guide. The design review supplies context; its suggested application architecture is outside this generator's scope.

## Generate the default dataset

Open a terminal in this project directory:

```bash
cd /home/ayush/Desktop/Hackathons/Hackathon
python3 scripts/generate_data.py
python3 scripts/validate_data.py
```

Both commands should finish with `VALIDATION: PASS`. Generation automatically validates the files, so the second command is useful after you edit a CSV.

Files are written to `data/synthetic/`:

| File | Default rows | Contents |
| --- | ---: | --- |
| `orders.csv` | 150 | Product, quantity, destination, due date |
| `plants.csv` | 9 | Eligible plant/product combinations and economics |
| `transport_duties.csv` | 36 | All 3 plants × 12 destinations |
| `capacity_events.csv` | 1 | Plant C loses 65% capacity for two weeks |
| `data_card.json` | — | Seed, parameters, dates, units, limitations, validation results |
| `validation_report.json` | — | Errors, warnings, counts, challenge-case audit |

There are **3 plants**, despite 9 plant rows: each row describes one eligible product at a plant. Generated files are ignored by Git. Re-running into the same output directory replaces these six files; use `--output` to keep separate datasets.

## Change the dataset size

A larger practice pack:

```bash
python3 scripts/generate_data.py --orders 500 --plants 5 --products 6 --destinations 18 --events 4 --weeks 4 --seed 42 --output data/stress_500
python3 scripts/validate_data.py --data-dir data/stress_500
```

A small pack for debugging:

```bash
python3 scripts/generate_data.py --orders 40 --plants 4 --products 3 --destinations 6 --events 1 --output data/debug
python3 scripts/validate_data.py --data-dir data/debug
```

A different planning start:

```bash
python3 scripts/generate_data.py --start-date 2026-11-02 --seed 123 --output data/november
python3 scripts/validate_data.py --data-dir data/november
```

The validator reads the planning start from `data_card.json`. The generator requires a Monday so production weeks have clear boundaries.

| Option | Default | Meaning |
| --- | ---: | --- |
| `--orders` | 150 | Exact number of orders |
| `--plants` | 3 | Number of plants; labels A, B, C, D… |
| `--products` | 4 | Total product types, including the exclusive product |
| `--destinations` | 12 | Distinct destinations in orders |
| `--events` | 1 | Exact number of capacity-loss events |
| `--weeks` | 4 | Production weeks |
| `--seed` | 20261011 | Reproducible random seed |
| `--start-date` | 2026-10-12 | First production Monday |
| `--output` | automatic | Folder for generated files |

Without `--output`, the default dimensions use `data/synthetic/`; changed dimensions use `data/synthetic_custom/`. A changed seed or start date alone still uses `data/synthetic/`.

Use the same seed, options **and code version** to reproduce identical CSVs. This refactor changes the generation algorithm; old and new versions can produce different CSVs with the same seed.

Supported minimums: 3 plants, 3 products, 4 destinations, 1 event, 3 weeks, and at least `max(12, destinations + 7)` orders. Some combinations need more orders to fit the exclusive-product scenario and cover all destinations; the CLI explains the error.

The participant guide requires plants A/B/C, at least 100 orders, 8 destinations, 3 cheapest-but-late orders, and a capacity-loss event. Smaller packs and extra plants are useful tests but receive warnings and `guide_minimums_met: false`. `valid: true` means the data checks passed; it does not by itself mean the guide minimums were met.

## Understand and modify the code

There is one generation path for every size:

```text
CLI options → build_tables() → four table builders → write CSVs/data card → validate
```

| File/function | What to change here |
| --- | --- |
| `scripts/generate_data.py` → `main()` | CLI defaults |
| `build_tables()` | Plant labels, product names, destinations, weekly capacities, carbon factors |
| `build_plants()` | Product eligibility, production cost, energy consumption, electricity price, weekly carbon changes |
| `build_transport()` | Shipping costs, transit days, import duties, transport emissions |
| `build_orders()` | Order quantities, due dates, named late orders, exclusive-product demand |
| `build_events()` | Disruption plant, date range, capacity loss |
| `generate_dataset()` | File writing and data-card descriptions |
| `scripts/data_schema.py` | Annex B column lists, CSV helpers, cost calculation, challenge-case audit |
| `scripts/validate_data.py` | Data checks and validation CLI |
| `scripts/flexible_data.py` | Compatibility wrapper for old imports; no generation rules |

For example, to make Plant A's ordinary shipping slower, change its `transit` value in `build_transport()`. To change random ordinary-order quantities, edit `rng.randint(18, 42)` in `build_orders()`. If you change the deliberate scenario rules, update their audit and data-card descriptions too. Keep the required Annex B field names unchanged.

The default pack keeps the familiar A/B/C capacities (840/760/350), `P_ONLY_C`, and 20 exclusive orders of 58 units. Other sizes scale capacity and use `P_EXCLUSIVE` at the last plant. All ordinary products remain eligible at A and B. Every market appears in the orders, and every plant has a route to every market.

## Deliberate challenge cases

1. `O-LATE-001`, `O-LATE-002`, `O-LATE-003`: A is cheapest but arrives late; another eligible plant arrives on time in week 1.
2. Exclusive product: only the last plant can manufacture it.
3. Germany/P1: A is cheaper than B before duty, but its 65% import duty reverses their cost ranking.
4. The last plant has the lowest carbon intensity and lowest capacity.
5. A 65% capacity loss affects weeks 2 and 3. In the default pack, exclusive demand is 1,160 units; normal capacity is 1,400 units, while disrupted capacity is 944 units. At least 216 units cannot be completed on schedule. Whole-order restrictions may increase the shortage.
6. Optional columns change carbon intensity in production weeks 2 and 3.

For custom packs, exclusive orders are constructed to fit into baseline weeks before the disruption. The validator audits the shortage after the disruption. It does not optimize the allocation.

## Units and assumptions

These are conventions for our synthetic data; confirm the corresponding rules when working with the organizer's pack.

- Capacity is **units per plant per week**, shared across products. The value repeats on plant/product rows: **do not sum those repeated values**.
- Each whole order is made at one plant in one production week. Production finishes Sunday; arrival is that Sunday plus `transit_days`.
- Capacity-event dates are inclusive. Events overlapping a week reduce that whole week's capacity; overlapping events use the largest loss, and capacity rounds down to whole units. Generated events align to full weeks.
- Costs use illustrative currency units. Production cost excludes the separate energy cost.
- Percentages are decimal fractions: `0.65` means 65%.
- Carbon uses kg CO2e; energy uses kWh.

The calculation conventions are:

```text
energy_cost_per_unit = energy_kwh_per_unit × energy_price_per_kwh
subtotal_per_unit = unit_cost + energy_cost_per_unit + transport_cost_per_unit
landed_cost = qty × subtotal_per_unit × (1 + duty_pct)
carbon_kg = qty × (energy_kwh_per_unit × week's carbon_kg_per_kwh + carbon_kg_per_unit)
```

`plants.csv` includes three optional fields allowed by Annex B: `energy_price_per_kwh`, `carbon_kg_per_kwh_week_2`, and `carbon_kg_per_kwh_week_3`. Use the weekly override for weeks 2/3 and the base carbon factor otherwise. The guide's fixed minimum schema omits energy price and a weekly-carbon representation, so these extra fields make those cases explicit. All other CSVs use only required columns.

## Validation and tests

```bash
python3 -m unittest discover -s tests -v
```

The tests cover default and custom sizes, reproducibility, required challenge cases, corrupted inputs, missing routes, optional columns, data cards, and planning-date inference. An independent scheduling test constructs an on-time baseline for the default seed; it does not claim minimum cost or feasibility for every custom dataset.

Validation checks required columns, nonempty required values, unique keys, integer quantities, positive and consistent capacities, finite nonnegative numeric values, eligibility, route coverage, dates, event plants/loss fractions, and data-card row counts. Errors include the file and row where possible. A failed validation exits with code 1.

To validate another Annex B pack without our data card:

```bash
python3 scripts/validate_data.py --data-dir /path/to/csv_folder --start-date 2026-10-12
```

The four files are required; `capacity_events.csv` may contain only its header before the late data drop. Optional energy/carbon columns are accepted, but aren't required for basic schema validation. Our synthetic challenge audit runs only for packs carrying our generator metadata. The validator reads files and reports results; it is not the application's database loader or allocation validator.

This is procedural synthetic data with no real company or personal records. Codex assisted the review/refactor and documentation; validation and automated tests check the resulting code. Each generated pack includes its own JSON data card, so no separate Markdown data card is needed.
