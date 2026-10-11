# Evaluation report

Cases: **28 / 28 passed**. Method: exact match on `answer_type` and the per-case checks in `eval/cases.yaml` (no LLM-as-judge). LLM mode: `mock`.

## Results by case

| Case | Category | answer_type | Pass | Latency ms | Notes |
|---|---|---|---|---|---|
| alloc-baseline | allocation | allocation | ✅ | 899 |  |
| trap-cheapest-late-O005 | cheapest_late | allocation | ✅ | 815 |  |
| trap-cheapest-late-O012 | cheapest_late | allocation | ✅ | 520 |  |
| trap-cheapest-late-O019 | cheapest_late | allocation | ✅ | 710 |  |
| explain-O077 | cheapest_late | allocation | ✅ | 144 |  |
| single-plant-O090 | allocation | allocation | ✅ | 726 |  |
| no-on-time-option-flagged | allocation | allocation | ✅ | 717 |  |
| cheapest-on-time | allocation | allocation | ✅ | 3204 |  |
| weights-cost-only | weights | allocation | ✅ | 2373 |  |
| weights-greener | weights | allocation | ✅ | 2729 |  |
| weights-carbon-only | weights | allocation | ✅ | 10437 |  |
| tradeoff-10pct | tradeoff | scenario | ✅ | 5880 |  |
| tradeoff-frontier | tradeoff | scenario | ✅ | 6146 |  |
| baseline-for-replans | allocation | allocation | ✅ | 716 |  |
| replan-B-30-event | capacity_loss | scenario | ✅ | 3536 |  |
| replan-C-50-weeks-1-2 | capacity_loss | scenario | ✅ | 745 |  |
| replan-A-40 | capacity_loss | scenario | ✅ | 10582 |  |
| replan-B-100-impossible | capacity_loss | scenario | ✅ | 11076 |  |
| infeasible-unmakeable-product | infeasible | infeasible | ✅ | 517 |  |
| infeasible-oversized-order | infeasible | infeasible | ✅ | 501 |  |
| refuse-ignore-capacity | guard | refused | ✅ | 35 |  |
| refuse-ignore-eligibility | guard | refused | ✅ | 31 |  |
| clarify-cut-carbon | guard | clarification_needed | ✅ | 32 |  |
| clarify-which-plant | guard | clarification_needed | ✅ | 32 |  |
| brief-faithful | brief | brief | ✅ | 149 |  |
| invent-savings | invent_numbers | refused | ✅ | 31 |  |
| invent-unasked-figure | invent_numbers | brief | ✅ | 147 |  |
| number-guard-catches-fabrication | invent_numbers | — | ✅ | 0 |  |

## Metrics

| Metric | Value |
|---|---|
| Plan validity (validator on every plan returned) | 100% of 22 plans |
| Solver plan, 70/30 (cost / carbon / late) | 1,659,698 / 179.4 t / 2 |
| Greedy cheapest-plant plan (cost / carbon / late) | 1,617,691 / 233.0 t / 114 |
| Greedy plan passes the never-late validator? | no — 112 violations (capacity ok: True) |
| Re-plan quality (Δcost, Δcarbon, % orders unchanged) | +7,889 / -0.3 t / 90%; -1,762 / +1.4 t / 81%; +41,700 / -23.6 t / 75%; +35,047 / +2.1 t / 44% |
| Brief faithfulness | 2 / 2 briefs pass the number check |
| Latency p50 / p95 | 717 ms / 10582 ms |
| LLM calls per request (mean) | 0.00 |
| Tokens per request (mean) | 0 |

## Configuration comparison: solver backend (70/30, never late)

| Backend | Status | Total cost | Carbon t | Valid | Solve ms |
|---|---|---|---|---|---|
| cpsat | OPTIMAL | 1,659,698 | 179.4 | True | 578 |
| pulp | OPTIMAL | 1,659,706 | 179.4 | True | 2096 |

Choice: CP-SAT is the default backend; see the timings above. CBC is kept as a cross-check of the optimum.
