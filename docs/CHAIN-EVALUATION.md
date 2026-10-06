# Complete Swap–Teleport comparison

This campaign compares **the entire workflow** in every model: native swapping
at R, delivery of the swap result, B correction, B–R–A pair-readiness delivery,
native teleportation at A using that same physical pair, A–R–B result delivery,
and final B correction. Latency ends at usable Bob output. Fidelity compares
that final output with the originally prepared input.

The earlier [swapping-stage campaign](REPRODUCING.md) remains reproducible,
but its ablation numbers must not be used as complete-workflow prediction errors.

## Why these workflow-start intervals?

A serial R BSM takes 27.405 μs. We control the time between **workflow starts**,
not the BSM itself. Every workflow executes both complete BSM instruction chains.

| Start interval | Actual spacing | Controlled effect at R |
|---|---:|---|
| 0.5× the R BSM time | 13.703 μs | The next workflow arrives before R finishes: R waiting grows |
| 1× the R BSM time | 27.405 μs | Arrival matches service completion: no R waiting and no spare service time |
| 2× the R BSM time | 54.810 μs | R finishes before the next arrival: no R waiting, with spare service time |

The half-time interval rounds 13,702.5 ns up to 13,703 ns. With four workflows,
Full Sync R waits are 0, 13.702, 27.404 and 41.106 μs in that condition. These
intervals create controlled contrasts around a known service time. They are
**not measured application arrival rates** or claims about steady-state queue
stability. They do not disable waiting at A/B or in the packet network.

In particular, removing R waiting can move waiting to A rather than reduce the
final completion time. That is why the comparisons now finish the whole chain.
Both error figures show the 0.5× start interval, where R waiting occurs, to keep
the three-model comparison compact. The 1× and 2× controls remain in the result
tables and CSVs. In these controls, No R contention and Decoupled timing agree
with Full Sync; Fixed classical delay can still introduce error.

## Models

| Model | Complete-workflow behavior |
|---|---|
| Full Sync | Production Q2NS/ns-3 packet execution, native NetSquid states and shared R/A/B FIFO processors |
| No R contention | Separate native R execution engines for each workflow; same logical memory resources, same shared A/B processors, actual packets regenerated at both new BSM completion times |
| Fixed classical delay | Shared native R/A/B processors; replace swap-result, pair-ready and teleport-result transport with three separately calibrated constant end-to-end delays |
| Decoupled timing | Hold each workflow's three path delays at its No R contention measurements; execute native NetSquid operations with shared R/A/B FIFOs and measure the final state |

Fixed delay uses virtual delivery events and makes no claim to execute actual
Q2NS/ns-3 packets. It still performs native quantum-channel delivery, actual
pair handoff and timed quantum operations. Decoupled uses the same native quantum
execution, but its per-workflow path delays never respond to the shifted packet
generation times. Its native start/completion times must exactly reproduce the
independent FIFO replay. Both Full Sync and No R contention
use the existing native Q2NS packet path throughout the complete chain.

No model may create a replacement EPR for teleportation. Every state-producing
model is cross-validated against a separate native NetSquid circuit at its own
observed operation times. A/B contention is retained and validated, not rejected.

## Run and render

Complete [installation](INSTALL.md) first. From the ns-3 root:

```bash
./ns3 build cosim-chained cosim-provisioned q2ns-test test-runner -j 2
"$QUCL_PYTHON" contrib/cosim/scripts/run_tests.py
"$QUCL_PYTHON" contrib/cosim/experiments/run_chain_evaluation.py \
  contrib/cosim/experiments/chain-profile.json --workers 1 \
  --output-dir results/qucl-chain-evaluation

"$QUCL_PLOT_PYTHON" contrib/cosim/experiments/render_chain_evaluation.py \
  results/qucl-chain-evaluation --output-dir results/qucl-chain-figures
```

`QUCL_PYTHON` contains NetSquid. `QUCL_PLOT_PYTHON` uses the separate plotting
environment described in [plotting setup](REPRODUCING.md#generate-the-figures).
The plotter needs no NetSquid installation. A quick smoke campaign is available
with `--quick`. `--resume` requires identical execution sources, plan and binary
hashes. Completed campaigns and figure outputs cannot be overwritten.
Start with one worker on a memory-constrained machine. Each additional worker
holds its own NetSquid execution and independent-reference data. If a run is
interrupted, use the same command with `--resume`; completed native reports from
that campaign are retained and the remaining executions are run normally.

The [profile](../experiments/chain-profile.json) preserves the literature-based
gate times, 20/10-ms T1/T2 memory, six input states, 100-Mb/s links, four loads,
three start intervals and 16 loaded traffic phases. The
[parameter sources and assumptions](REPRODUCING.md#quantum-profile-and-its-sources)
are unchanged. Four disjoint calibration phases per loaded condition (one at
zero load) supply the **three** path means. Calibration has no processor waiting,
and its complete workflows finish while background traffic is active.
The calibration workload is sparser than the test batches. Fixed-delay error
therefore includes both a possible difference in mean path delay between these
workloads and the variation removed by using constants. Its error is conditional
on this calibration design; it does not rank every possible delay approximation.

A full grid has 147 traffic conditions × 6 inputs × 4 state-producing models:
**3,528 complete-chain simulation runs**, plus **13 calibration runs** and the
separate six-state execution validation. Each model evaluates 3,528 workflows.
Decoupled now produces final native states and branch-weighted fidelities as well
as timings. No previous campaign's report is reused by a fresh invocation. Within a worker, identical
independent-reference specifications may reuse a bounded in-memory result. The
key includes input, noise, instruction times and outcomes; every co-simulation
and every native checkpoint comparison still runs.

### Add Decoupled state execution to an existing three-model campaign

The older complete-chain campaign already contains valid Full Sync, No R
contention and Fixed classical delay results. To retain them and execute only
the missing model, use a new output directory:

```bash
"$QUCL_PYTHON" contrib/cosim/experiments/run_chain_evaluation.py \
  contrib/cosim/experiments/chain-profile.json --workers 1 \
  --extend-from results/qucl-chain-evaluation \
  --output-dir results/qucl-chain-all-states
```

The extension verifies every parent report hash, workload, runtime version and
participant binary. It preserves 2,646 evaluation runs, 13 calibration runs and
the existing validation, then adds **882 Decoupled native runs**. Parent reports
remain unchanged. `extension.json` and `parent-summary.json` record their origin;
the new summary distinguishes retained runs from new executions. An interrupted
extension resumes with `--resume` instead of `--extend-from`.

All previous latency errors must remain identical, and all previous Full Sync,
No R contention and Fixed classical delay state results must remain identical.
The new Decoupled fidelities come from new native executions and independent
16-branch validation, never from another model's state or total latency.
An independent reference already calculated for an exactly identical input,
noise model, all operation/arrival times and measurement outcomes can be memoized.
The new coupled execution and every native checkpoint comparison still run;
`reference_cache` records these exact matches in each Decoupled report.

## Metrics and statistical units

Each state-producing run enumerates all 16 joint measurement outcomes using
independent native NetSquid circuits at its observed instruction times. Branch
fidelities use NetSquid's `fidelity(..., squared=True)`; the probability-weighted
mean is kept separate from the live sampled fidelity. Timing must be identical
across the six inputs within each model and traffic condition.

An error pairs the same input, workflow and traffic phase between a model and
Full Sync. Fidelity error refers to **final teleportation output**, not the
intermediate swapped pair. MAE takes the absolute paired difference first,
then averages six inputs and four workflows within a phase. Bias retains the
sign (model minus Full Sync). The phase means are resampled 10,000 times for
95% percentile bootstrap intervals. The six inputs and four workflows are not
independent traffic samples. Zero load is deterministic and has no bootstrap
whisker. Calibration uncertainty is not included.

All inputs and elementary EPRs originate at t=0, so later workflow starts also
change pre-request storage age. Across-interval comparisons do not isolate
processor-wait noise; paired model comparisons and within-interval load contrasts
hold those birth times fixed. This remains a finite-batch, fixed-topology,
composite abstract hardware study.

## Outputs

| Output | Meaning |
|---|---|
| `figures/fig3_chain_latency.pdf` / `.png` | Full Sync complete-chain added latency |
| `figures/fig4_teleport_fidelity.pdf` / `.png` | Full Sync final teleportation fidelity |
| `figures/fig5_a.pdf` / `.png` | Complete-chain latency MAE at the 0.5× start interval |
| `figures/fig5_b.pdf` / `.png` | Final teleportation fidelity MAE at the 0.5× start interval |
| `model_error_cells.csv` | Both MAEs, signed fidelity bias and confidence intervals |
| `model_predictions.csv` | Input/workflow-resolved final latency and expected fidelity |
| `chain_cells.csv` | Full Sync means and intervals |
| `execution_changes.csv` | Paired changes in R/A/B waits and all three path delays; their sum is checked against total latency change |
| `RESULTS.md`, `FIGURES.pdf`, `provenance.json` | Readable results, combined figures and provenance |

The renderer verifies the raw report hashes, recalculates each paired comparison,
checks CSV values, repeats the phase aggregation and verifies calibration.
Figures use the same pastel model colors throughout, with hatches. Each error
figure has one panel, with all three models across the four loads. No chart
titles are added; the caption identifies the 0.5× start interval.

Implementation: [runner](../experiments/run_chain_evaluation.py),
[models](../experiments/chain_models.py),
[metrics and timing replay](../experiments/chain_analysis.py),
[renderer](../experiments/render_chain_evaluation.py),
[tests](../tests/test_chain_evaluation.py).
