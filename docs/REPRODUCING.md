# Reproduce the execution-coupling experiments

This campaign reproduces the complete Swap → A/B Teleport comparison and the
swapping-stage ablations used for Figures 3–5. It runs the public QuCl execution
core, Q2NS applications and actual ns-3 result packets. Manuscripts and previously
generated reports are not required.

The [example scenarios](../scenarios/) use slower illustrative defaults. The
[experiment profile](../experiments/profile.json) explicitly overrides them.
Do not substitute the example defaults when reproducing this campaign.

## Code and configuration

| File | Purpose |
|---|---|
| [profile.json](../experiments/profile.json) | Complete quantum, network, workload, calibration and statistical settings |
| [run_evaluation.py](../experiments/run_evaluation.py) | Validation, independent calibration, six-input chain runs and paired ablations |
| [models.py](../experiments/models.py) | Full Sync, No R contention and Fixed classical delay execution |
| [timing.py](../experiments/timing.py) | Independent timing checks and Decoupled timing prediction |
| [analysis.py](../experiments/analysis.py) | Per-session errors, balanced input means and phase-bootstrap intervals |
| [render_evaluation.py](../experiments/render_evaluation.py) | Report/CSV audit, aggregation and three figures |
| [test_evaluation.py](../tests/test_evaluation.py) | Production equivalence, model isolation, failure checks and statistical-unit tests |

The two C++ participants remain [cosim-chained.cc](../examples/cosim-chained.cc)
and [cosim-provisioned.cc](../examples/cosim-provisioned.cc). The campaign uses
[run_chained.py](../python/run_chained.py), and its Full Sync ablation uses the
same provisioned core as [run_provisioned.py](../python/run_provisioned.py).

## Run the campaign

Complete [installation](INSTALL.md), including the Q2NS patches. Run from the
ns-3 root; `QUCL_PYTHON` is the interpreter containing NetSquid 1.1.7.

```bash
./ns3 build cosim-chained cosim-provisioned q2ns-test test-runner -j 2
"$QUCL_PYTHON" contrib/cosim/scripts/run_tests.py

"$QUCL_PYTHON" contrib/cosim/experiments/run_evaluation.py \
  contrib/cosim/experiments/profile.json \
  --workers 4 --output-dir results/qucl-evaluation
```

For a smaller end-to-end smoke campaign, use a **different output directory**:

```bash
"$QUCL_PYTHON" contrib/cosim/experiments/run_evaluation.py \
  contrib/cosim/experiments/profile.json --quick \
  --output-dir results/qucl-evaluation-quick
```

Quick mode retains all three start intervals, six inputs and validation checks,
but uses loads 0 and 0.75, two test phases and one calibration phase. It does not
reproduce the full-grid confidence intervals.

`--workers` accepts 1–8 independent condition processes (default 1). Each uses
its own NetSquid simulator, ns-3 participants and reference processes. Results
are collected in the same condition order, so worker count does not change the
statistical units or random phases. Use fewer workers if memory is limited.

Completed output directories cannot be overwritten. An interrupted campaign can
be continued with `--resume` and the same arguments, including `--quick` if used.
The plan, execution-source hashes, participant binaries and runtime environment
must match. Keep the partially completed reports intact. Use a new directory
after changing code or parameters. A failed simulation retains its failure report;
a `summary.json` is written only after the complete campaign passes.

## Generate the figures

Plotting is separate from the NetSquid environment. From the ns-3 root, create
an optional Python 3.10 environment with the pinned plotting dependencies:

```bash
python3 -m venv "$HOME/.venvs/qucl-plot"
export QUCL_PLOT_PYTHON="$HOME/.venvs/qucl-plot/bin/python"
"$QUCL_PLOT_PYTHON" -m pip install \
  -r contrib/cosim/experiments/requirements-plot.txt

"$QUCL_PLOT_PYTHON" contrib/cosim/experiments/render_evaluation.py \
  results/qucl-evaluation --output-dir results/qucl-figures
```

The renderer checks report and CSV hashes, traces every chain metric back to its
report, verifies branch probabilities and recomputes the aggregates. It produces:

| Output | Meaning |
|---|---|
| `figures/fig3_chain_latency.pdf` / `.png` | Mean complete-chain latency above the zero-load, longest-interval reference |
| `figures/fig4_teleport_fidelity.pdf` / `.png` | Absolute final teleportation fidelity; expanded vertical axis |
| `figures/fig5_model_error.pdf` / `.png` | Swapping-stage latency MAE against Full Sync at the shortest interval |
| `chain_cells.csv` | Means and 95% intervals for latency, fidelity and latency components |
| `result_phase_coupling.csv` | Per-session change in result delay when R waiting is removed |
| `RESULTS.md`, `provenance.json`, `FIGURES.pdf` | Readable summary, source/data hashes and combined figures |

Figures use compact dimensions, pastel blue/orange/pink, distinct hatches or
markers, and short axis labels. No plot titles are added. Generated output stays
outside Git; the scripts and profile are the reproducibility artifact.

## Quantum profile and its sources

| Parameter | Value | Basis |
|---|---:|---|
| CNOT | 20 μs | Gate-time profiles in [Liao et al. (2022)](https://doi.org/10.1038/s41598-022-08901-x) and [Iqbal et al. (2023)](https://doi.org/10.3390/s23187891), Table 3 |
| H, X, Z | 5 ns | Same gate-time profiles |
| Measurement | 3.7 μs | Same gate-time profiles |
| Memory T1 / T2 | 20 / 10 ms | Values in [Bugalho et al. (2023)](https://doi.org/10.1038/s41534-023-00773-x), Fig. 3a |
| Gate depolarization probability | 0.01 | Noise level used in Iqbal et al., Section 4; placement below is the explicit QuCl model |
| Serial CNOT–H–M–M duration | 27.405 μs | Sum of the implemented native instructions |
| X-or-I and Z-or-I correction slots | 10 ns total | Two fixed 5-ns slots, independent of the measurement outcome |

References: Liao et al., *Benchmarking of quantum protocols*, Scientific Reports
12, 5298 (2022); Iqbal et al., *Investigating Imperfect Cloning for Extending
Quantum Communication Capabilities*, Sensors 23(18), 7891 (2023); Bugalho et al.,
*Resource-efficient simulation of noisy quantum circuits and application to
network-enabled QRAM optimization*, npj Quantum Information 9, 105 (2023).

These sources form a **composite abstract model**, not a calibration of one
hardware platform. Memory parameters apply uniformly to all positions. Native
T1/T2 aging covers idle and active storage. Independent depolarization acts on
each CNOT/H/X/Z operand after its storage interval and before the gate action.
Identity slots and ideal measurements receive storage noise only. Sources and
readout are ideal; quantum transit is lossless and noiseless. Delivered halves
start memory aging when placed in their destination memory.

## Workload and statistical units

- Four workflows per batch. Inputs and elementary EPRs are born at time zero;
  quantum delivery takes 0.8 ms to A and 1.2 ms to B. Local starts begin at 2 ms.
- Start intervals: **13.703, 27.405 and 54.810 μs**, corresponding to 0.5×, 1× and
  2× the BSM duration, with the half interval rounded up by 0.5 ns. Every BSM
  executes all instructions. These factors describe workflow spacing only.
- Classical links: 100 Mb/s and 100 μs propagation per hop. Background traffic
  uses 1000-byte payloads (1030 wire bytes) on R→B. Its offered loads are
  **0, 0.25, 0.50 and 0.75**; protocol traffic is additional.
- Background period is 82.4 μs divided by load, rounded to ns. Test seeds
  **1000–1015** choose random start phases; quantum seed is 7. Zero load runs
  once per interval because traffic phase is inactive.
- Each traffic condition reruns the full four-workflow batch separately for
  `0`, `1`, `+`, `-`, `+i`, and `-i`. Native timings must match across inputs.
- Calibration uses **100–103**, disjoint from test phases, with 20 BSM durations
  between requests. Every calibration transaction finishes while background
  traffic is still active. Fixed delay is the measured mean result delay,
  rounded to integer ns with ties up.

The full grid has **147 paired conditions**, **882 chain runs**, **441 ablation
runs**, and **13 calibration runs**. There are 3,528 evaluated chained workflows
and 588 transactions per ablation model. Validation executions are additional.

Average the four sessions and six input states **within each traffic phase**,
then bootstrap phase means 10,000 times for 95% percentile intervals. Sessions,
input states and measurement branches are not independent traffic samples.
Intervals are conditional on the fixed calibration; they do not include its
estimation uncertainty. Zero-load results have no traffic-bootstrap interval.

All resources are born together, so changing the request interval changes
pre-request storage age as well as processor waiting. Compare network loads at
a fixed interval to isolate the changed traffic condition. These finite batches
and periodic traffic do not establish steady-state performance or generality
across hardware and topologies.

## Fidelity and comparison models

`metrics.chains[*].output_fidelity` remains the **sampled native fidelity**.
The campaign also obtains all 16 joint measurement branches from the independent
NetSquid circuit at the observed operation times. Each branch uses native
`qubitapi.fidelity(..., squared=True)` against the originally prepared input.
Their probability-weighted mean is `expected_fidelity`; this is the quantity in
Figure 4. Sampled states are cross-validated at instruction and protocol
checkpoints. Input aging is included. This expectation is valid here because
fixed instruction slots and packet sizes make timing independent of outcomes.

| Model name in reports | Change from Full Sync |
|---|---|
| `Full-Sync` | Actual Q2NS/ns-3 result packets, shared R/B FIFO, native NetSquid instructions and aging |
| `No-Dq-R` | Per-session native R engines remove only R execution waiting; actual packets are regenerated at the new completion times |
| `Fixed-Dc` | Calibrated constant result delay replaces the classical packet path; R/B scheduling and quantum delivery remain native |
| `Decoupled` | Adds resource-eligible R FIFO waiting to the result delays measured in No-Dq-R; does not regenerate packets after adding that wait |

The ablations evaluate **swapping only**. They are not complete-chain predictions.
The campaign rejects B contention in these ablations so only R capacity is
relaxed. Decoupled predicts latency/deadline only: its fidelity and service
feasibility are undefined. The full chained workflow still permits shared A/B
processor waiting and checks it normally.

`chains.csv` contains sampled and expected fidelity separately. `errors.csv`
contains paired latency, fidelity and audit-threshold errors. Thresholds
F_min=0.5 and deadline=5 ms are retained for audit, not tuned primary outcomes.
Latency and deadlines begin at local session start. Zero observed classification
errors do not establish zero population error.

## Validation counts and evidence

The compact release suite and this campaign have different execution counts.
[test_chained.py](../tests/test_chained.py) uses 16 selected seeds for each of six
inputs: **96 noiseless native executions**. It also runs **12 noisy reference
checks**. The campaign instead runs all six inputs in each seed batch until all
16 joint outcomes per input have appeared; the original campaign required
**54 batches / 324 chains**, including repeated outcomes. Its actual new count
is recorded in `validation/summary.json`, never assumed by the runner.

The earlier development suite's 240 tests were 196 historical regressions plus
44 evaluation tests. Those counts are not the compact public release suite.
Current test counts come from `scripts/run_tests.py`; campaign run counts come
from its completed `summary.json`. Keep these two sources separate when reporting
validation coverage.

The reproduction extension passed **52 Python tests (37 existing + 15 new) and
83 Q2NS native cases** in a separate ns-3.47 build. The added tests also verify
that a spawned condition process returns the same results as sequential execution.

The full profile was rerun on 2026-10-05: **1,402 raw reports passed validation**
(1,323 evaluation, 13 calibration, 54 noiseless batches and 12 noisy checks).
Maximum reference density-matrix error was **9.99 × 10⁻¹⁶**. All 3,528 chained
workflow latencies and expected fidelities matched the preserved campaign, as
did calibration and the ablation latency means and bootstrap intervals.

Useful numerical checks for this exact profile are:

| Check | Reproduced value |
|---|---:|
| Zero-load chain latency at the 1× interval | 576.110 μs |
| Six-input mean fidelity in that condition | 0.717247 |
| No R contention latency MAE, load 0.75 / 0.5× interval | 17.28 μs |
| Fixed classical delay latency MAE, same condition | 23.40 μs |
| Decoupled timing latency MAE, same condition | 26.44 μs |

These are configuration-specific checks, not general performance guarantees.
Full precision, intervals and individual observations are generated by the scripts.

Every completed campaign records its plan, execution-source and binary hashes,
runtime versions, reference errors, and raw-report hashes. Generated traces,
figures, manuscript files and large historical archives are not committed.
