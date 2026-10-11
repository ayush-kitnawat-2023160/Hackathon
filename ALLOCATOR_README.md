# Standalone allocation engine

The engine uses Python and **PuLP 3.3.0 with CBC**. It accepts CSVs and structured settings, calculates costs and carbon, assigns whole orders, and checks the result independently. Agents, APIs, and databases can call it later.

The participant guide's Annex A/B requirements are the reference for capacity, eligibility, delivery, costs, and carbon. The supplied design documents propose implementation choices; their agent architecture is outside this component's scope.

## Which suggested improvements belong here

| Decision | Improvements | Reason |
| --- | --- | --- |
| Added | Sampled carbon targets, simple infeasibility checks, counterfactual runs, fixed commitments | They explain required trade-offs, failure, and replanning with the existing solver |
| Already present | Verified solver status, stable weight references, best-effort policy, scenario comparison, timing | These were implemented and tested before this extension |
| Later | Customer priority/revenue rules, expansion advice, automatic relaxation, uncertainty models, advanced factory scheduling | The guide does not supply the needed business values or policies; these would increase scope during the hackathon |

There is no extra agent framework or optimizer. The new operations are ordinary Python functions.

## Install and run

```bash
cd ~/Desktop/HCL-Hackathon/Delta-Demo-Project
python3 -m pip install --target .allocator-deps -r requirements-allocator.txt
python3 scripts/generate_data.py
python3 scripts/run_allocator.py --no-fallback
```

Other useful commands:

```bash
python3 scripts/run_allocator.py --objective carbon
python3 scripts/run_allocator.py --objective balanced --cost-weight 0.7 --carbon-weight 0.3
python3 scripts/run_allocator.py --apply-events
python3 scripts/run_allocator.py --compare --json-output data/outputs/replan.json
python3 scripts/run_allocator.py --carbon-cut-pct 10
python3 scripts/run_allocator.py --config-file examples/allocator_request.json
python3 scripts/run_allocator.py --data-dir data/synthetic_custom --time-limit 5
```

`--config-file` overrides objective, weight, budget, and time-limit CLI flags. `--apply-events` and `--no-fallback` still override the corresponding booleans. `--compare` removes both file and inline events from the baseline and applies them to the scenario. Carbon-cut runs retain supplied exclusions, events, and budgets, but use a cost objective and strict delivery.

For organizer data without a data card or energy-price column:

```bash
python3 scripts/run_allocator.py --data-dir /path/to/csvs \
  --start-date 2026-10-12 --weeks 4 --energy-price-default 8
```

That explicitly assumes a tariff of 8 currency units/kWh. Confirm the tariff for official data. A missing capacity-events file is allowed before the late drop. When a data card includes row counts, the loader checks them.

## What the solver does

One binary variable represents each eligible order/plant/week choice. In strict mode:

```text
For each order: sum of its choices = 1
For each plant/week: sum of assigned quantities <= available capacity
Only on-time choices are allowed
Optional total cost and carbon budgets remain hard constraints
```

The engine removes choices that cannot fit an entire order. Constraint names use numeric indexes, so punctuation in order IDs cannot create name collisions.

`frozen_assignments` fixes both plant and week for committed orders. It uses the same order-to-`[plant, week]` shape as `previous_plan`. If a disruption makes a commitment impossible, the result is infeasible; the commitment is never silently moved or dropped. `previous_plan` with `stability_weight` remains a softer preference for orders that can move.

Cost mode minimizes landed cost; carbon mode minimizes emissions. Balanced mode minimizes:

```text
cost_weight × (cost / cost_reference) + carbon_weight × (carbon / carbon_reference)
```

Weights are normalized to sum to one. References are sums of per-order minimum on-time values before exclusions and disruptions, with a floor of 1. They are lower bounds, not a feasible baseline plan. They remain fixed when capacity events change, and are included in `objective_references`. The 70/30 default favors cost while allowing carbon to influence choices; it is a preference, not a promised carbon cut. A carbon-cut request uses an actual emissions cap derived from a proven cheapest baseline.

`previous_plan` and `stability_weight` can discourage changes to plant/week assignments. This is an optional preference; it can trade cost or carbon for stability and cannot bypass hard constraints.

## Failure and fallback behavior

| Status | Meaning |
| --- | --- |
| `OPTIMAL` | Strict, on-time allocation; CBC proved optimality and validation passed |
| `FEASIBLE` | Strict verified allocation; optimality was not proven |
| `BEST_EFFORT` | Verified physical/budget constraints, with possible lateness or unmet orders |
| `INFEASIBLE` | Constraints proved infeasible and no accepted plan is returned |
| `NOT_SOLVED` | No verified plan was found within the available time |
| `ERROR` | Solver output could not be accepted |

A timeout does not establish infeasibility and does not trigger relaxed delivery. Best effort starts only after strict infeasibility is proven. It minimizes these priorities in order, locking each proven optimum before the next solve:

1. Unallocated order count.
2. Unallocated quantity.
3. Late order count.
4. Total late days.
5. Cost/carbon objective.

The count-first policy favors fulfilling more orders before more units. Forced assignments are hard: those orders cannot be dropped to make the model feasible. Budgets and exclusions remain hard in fallback.

`time_limit_seconds` is shared across stages within one allocation call. CBC startup and input validation add small overhead. A comparison or carbon-cut operation makes two allocation calls. If a fallback stage times out, a verified incumbent can be returned with `proven_optimal: false` and a reason identifying the unfinished priority.

Failure results have `kpis: null` and `validator: null`; they do not imply zero cost or zero demand. CLI exit codes are 0 for verified results (including best effort), 1 for unconfirmed/infeasible results, and 2 for invalid inputs or setup errors.

CBC's broad PuLP status can label a time-limited incumbent "Optimal". The engine checks the separate solution status before making that claim; see the [PuLP CBC implementation](https://github.com/coin-or/pulp/blob/3.3.0/pulp/apis/coin_api.py).

## Assumptions and validation

- Orders stay whole at one plant in one week.
- Capacity is shared per plant, repeated on plant/product rows; never sum those repeated values.
- Production completes Sunday. Arrival is that Sunday plus transit days.
- Landed cost includes production, energy, shipping, and duty on their subtotal.
- Carbon includes manufacturing energy and transport. Optional weekly carbon factors override the base factor.
- Event dates are inclusive. Any overlap affects the whole week; the largest loss wins. Capacity rounds down, using decimal arithmetic to avoid losing an extra unit to floating-point error.
- Percentages are fractions: `0.30` means 30%.

Energy tariffs, duty base, dispatch timing, shared capacity, and partial-week treatment must be confirmed for organizer data. These are declared conventions, not established organizer business rules.

The loader rejects malformed rows, duplicate keys/columns, nonfinite or negative numbers, fractional integer fields, missing routes, unsupported products, inconsistent capacities, invalid dates/events, and data-card count mismatches. Typed configs and datasets are checked too.

The independent validator recalculates each row from raw input records. It verifies quantity, eligibility, arrival, reported amounts, capacity, budgets, exclusions, and forced assignments. Totals use unrounded coefficients; rounding is only for display. Unmet orders do not pass `delivery_ok`. Comparisons reject changed demand and warn when lower totals reflect unfulfilled orders.

Each result includes a `dataset_signature` of the input contents, plus its settings, solver stages, and elapsed time. The signature ignores the folder path. This helps identify which input a saved result belongs to.

## Decision checks

Three functions build on the tested solver:

```python
from app.allocator import (
    analyze_alternative, analyze_tradeoffs, diagnose_infeasibility,
    load_dataset, solve_allocation,
)

data = load_dataset("data/synthetic")
baseline = solve_allocation(data, {"objective": "cost"})

tradeoffs = analyze_tradeoffs(data, targets=[0, 5, 10, 20])
problem = diagnose_infeasibility(data, {"use_capacity_events": True})
alternative = analyze_alternative(data, baseline, "O-LATE-001", "A")

committed = solve_allocation(data, {
    "use_capacity_events": True,
    "frozen_assignments": {"O-LATE-001": ["B", 1]},
})
```

`analyze_tradeoffs()` minimizes cost under each requested carbon cap. `points` contains every requested target and its solver status. `sampled_frontier` keeps distinct, proven plans that are not dominated **among those sampled points**. A plan stopped by the time limit can appear in `points`, but cannot be presented as a proven frontier result. The baseline also has to be proven optimal.

`diagnose_infeasibility()` reports only clear necessary failures: no on-time option, too much demand tied to one plant before a deadline, or a budget below an independent lower bound. For the default outage, it identifies 1,160 exclusive-product units against 944 available at Plant C, a shortage of at least 216 units. It does not claim to explain every possible interaction between constraints, and a minimum shortage does not promise that adding exactly that much capacity is sufficient.

`analyze_alternative()` reads a verified strict plan, keeps its recorded settings, forces one order to the requested plant, and solves again. It reports cost, carbon, and other orders moved when the alternative is valid. `both_optimal` is true only when both runs proved optimality. It rejects changed input data and changes to frozen orders.

## Tests and evaluation

```bash
PYTHONPATH=.allocator-deps python3 -m unittest discover -s tests -v
python3 scripts/evaluate_allocator.py --repeats 3 --time-limit 10
```

The evaluation writes `eval/allocator_report.json`, which can be committed. It records test outcomes, full-pack cost/carbon/balanced/outage runs, validation rate, timings, p50/p95 latency, and a simple greedy baseline. Small exhaustive cases check the solver against every possible assignment, including the fallback priority order. Timeouts are also simulated in regression tests and exercised in the 500-order stress run.

This covers the deterministic engine. It does not replace the guide's complete application evaluation: API contracts, agent faithfulness, traces, UI, and Docker still need testing when integrated. The generated report states that limit.

## Code map

| File | Responsibility |
| --- | --- |
| `app/allocator/models.py` | Records and settings validation |
| `loader.py` | CSV loading and typed-input checks |
| `candidates.py` | Cost/carbon/arrival choices and disrupted capacity |
| `engine.py` | Model, solver statuses, fallback, comparison, carbon cuts |
| `analysis.py` | Trade-offs, bottleneck checks, counterfactuals |
| `validator.py` | Independent result checks |
| `scripts/run_allocator.py` | Command-line entry point |
| `scripts/evaluate_allocator.py` | Tests and measured evaluation report |

Library use:

```python
from app.allocator import load_dataset, solve_allocation

data = load_dataset("data/synthetic")
result = solve_allocation(data, {
    "objective": "balanced",
    "cost_weight": 0.7,
    "carbon_weight": 0.3,
    "time_limit_seconds": 10,
})
if (result.get("validator") or {}).get("valid"):
    print(result["kpis"])
else:
    print(result["status"], result["reason"])
```

Codex assisted the review, fixes, tests, and documentation. Correctness checks use manual examples, exhaustive comparisons, independent validation, and real solver runs. The code keeps one solver dependency and short comments so the model can be explained and changed during the hiring panel.
