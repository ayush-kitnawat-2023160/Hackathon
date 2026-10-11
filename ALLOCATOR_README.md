# Standalone Multi-Plant Allocation Engine (no Planning Agent)

This solver consumes Annex B data and a **structured config**. A teammate can later
connect it to LangGraph or FastAPI without modifying the optimizer.

## Install once

Python 3.11 or 3.12; PuLP ships a CBC solver binary. On Ubuntu without `python3-venv`, install locally:

```bash
cd ~/Desktop/HCL-Hackathon/Hackathon
python3 -m pip install --target .allocator-deps -r requirements-allocator.txt
```

Alternatively, use your own environment's `pip install -r requirements-allocator.txt`.

## Run

```bash
# Baseline (capacity_events.csv intentionally NOT applied)
python3 scripts/run_allocator.py --data-dir data/synthetic

# Run outage re-planning and compare both plans
python3 scripts/run_allocator.py --data-dir data/synthetic --compare \
    --json-output data/outputs/replan.json

# Apply outage explicitly (falls back to labeled BEST_EFFORT if infeasible)
python3 scripts/run_allocator.py --data-dir data/synthetic --apply-events

# Consume a structured config from a separate planning component
python3 scripts/run_allocator.py --data-dir data/synthetic \
    --config-file examples/allocator_request.json \
    --json-output data/outputs/allocator_result.json

# Change optimization objective
python3 scripts/run_allocator.py --objective carbon
python3 scripts/run_allocator.py --objective balanced --cost-weight 0.3 --carbon-weight 0.7

# Find cheapest strict plan with at least 10% less carbon than cheapest baseline
python3 scripts/run_allocator.py --carbon-cut-pct 10

# Stress test with 500 orders / 5 plants
python3 scripts/run_allocator.py --data-dir data/synthetic_custom --time-limit 30

# Judge-supplied files without data_card or optional electricity price column
python3 scripts/run_allocator.py --data-dir /path/to/judge_csvs \
    --start-date 2026-10-12 --weeks 4 --energy-price-default 8
```

The last command **explicitly assumes** an energy price of 8 synthetic currency units per kWh:
the official minimum CSV schema does not include this input. Confirm the real business
assumption with the organizers; do not silently assign it in production.
With `--config-file`, the JSON file takes precedence over the objective/weight/budget
CLI flags; `--apply-events` and `--no-fallback` can still override those two booleans.

To test:

```bash
PYTHONPATH=.allocator-deps python3 -m unittest discover -s tests -v
```

## How the engine works

1. `loader.py` loads the four CSVs and validates their required fields, numbers, routes, and eligibility.
2. `candidates.py` creates allowed order-plant-week options with actual arrival dates, landed costs and weekly carbon.
3. `engine.py` constructs a **binary mixed-integer linear optimization** model with PuLP/CBC.
4. Hard constraints: each whole order assigned exactly once (strict), valid product and plant, weekly shared plant capacity, and optional cost/carbon budgets or exclusions.
5. Strict optimization only includes candidates with on-time arrival. If infeasible and fallback enabled, a **best-effort** model can report late/unallocated orders without violating capacity/eligibility.
6. `validator.py` independently recalculates every assignment, cost, carbon, due date, and capacity; invalid results are **not** returned as successful allocations.
7. Scenario evaluation reuses exactly the same engine with changed weekly capacities; `compare_plans()` shows moved orders and KPI deltas.

### Important modeling assumptions

- Orders **cannot be split** between plants/weeks; each allocation decision is binary.
- Weekly plant capacity is **shared across products**, though repeated in each product row.
- Production completes at the **end of the selected week (Sunday)** and transportation begins then.
- Carbon is manufacturing electricity use times that week's carbon factor plus transport emissions.
- Cost includes production, electricity, transportation and import duty; the synthetic data assumes duty on their sum.
- Capacity events use **largest overlapping percentage loss** per plant-week, rounded down to whole units.
- Strict uses on-time-only choices; `BEST_EFFORT` is clearly marked as not fully on-time or not fully allocated.
- Defaults are deliberately **not applied** to the late-drop events until requested.
- `time_limit_seconds` is the limit for **each solver stage**, not a global wall-clock deadline.

## Pure-Python function for the teammate's agent

The agent should produce **validated JSON parameters**, not assignments:

```python
from app.allocator import load_dataset, solve_allocation

data = load_dataset("data/synthetic")
result = solve_allocation(data, {
    "objective": "balanced",             # cost | carbon | balanced
    "cost_weight": 0.7,
    "carbon_weight": 0.3,
    "use_capacity_events": False,
    "allow_fallback": True,
    "max_carbon_kg": None,
    "max_cost": None,
    "excluded_plants": [],
    "excluded_plant_products": [],       # e.g. [["A", "P2"]]
    "forced_assignments": {},            # e.g. {"O-LATE-001": "B"}
    "time_limit_seconds": 20,
})

if result.get("validator", {}).get("valid"):
    print(result["allocation"])
    print(result["kpis"])
else:
    print(result["status"], result.get("reason"))
```

With an **inline what-if event** (separate from `capacity_events.csv`):

```python
config = {
  "objective": "cost",
  "additional_capacity_events": [{
    "event_id": "WHAT_IF_B",
    "plant": "B",
    "from_date": "2026-10-19",
    "to_date": "2026-11-01",
    "capacity_loss_pct": 0.30
  }]
}
new_plan = solve_allocation(data, config)
```

**The Planning Agent is deliberately not implemented here.** The agent should never
create allocations or alter hard constraints directly. Only an approved config is accepted;
unsupported config keys, invalid numeric values, unknown plants/orders and invalid events
raise clear errors.

## Files

| File | Purpose |
|---|---|
| `models.py` | Immutable business records and typed config |
| `loader.py` | Read and validate Annex B input files |
| `candidates.py` | Cost/carbon/delivery option generation and weekly capacity |
| `engine.py` | Mathematical model, solving, best-effort and comparison |
| `validator.py` | Independent business constraint checking |
| `scripts/run_allocator.py` | CLI runner; optional JSON results |
| `tests/test_allocator.py` | Numerical, feasibility and scenario regression tests |
