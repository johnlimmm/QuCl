# Comparison with standalone simulators

Status: both actual standalone runners are implemented. The expanded comparison
uses 16 traffic offsets and the cost campaign uses ten fresh-process repetitions
per condition. Existing pilot and ablation results are preserved separately.
This comparison evaluates explicit model choices, not an intrinsic accuracy
ranking of the simulator products.

## Expanded comparison

The completed expansion uses [standalone-profile.json](../experiments/standalone-profile.json):
loads 0/0.75, all three start intervals, six inputs and sixteen evaluation offsets
at nonzero load. Five independent calibration runs and **1,530 new evaluation
runs** passed, with maximum reference matrix discrepancy **8.88e-16**.
The corresponding cost campaign completed **300 executions**, ten per
engine/size/load condition. These are fresh runs; pilot observations were not
pooled into the expanded estimates.

At the 0.5x interval and load 0.75, mean complete-workflow latency was
**688.73 us** (QuCl), **628.33 us** (NetSquid-only), and **565.17 us**
(Q2NS-only). With the matched noisy model, final expected fidelity was
**0.71395** (QuCl) and **0.71570** (NetSquid-only). The Q2NS configuration
has no matched noisy fidelity endpoint. Across all six shared load/interval
conditions, the independently rerun QuCl means reproduce the original
16-offset ablation: latency differences are zero and fidelity differences
are at most 2.22e-16. Repeating identical conditions does not add independent
traffic samples.

At 32 workflows and load 0.75, median total execution time was **2.728 s**
(QuCl), **1.375 s** (NetSquid-only), and **0.570 s** (Q2NS-only).
The interquartile ranges were [2.579, 2.867], [1.323, 1.536], and
[0.534, 0.624] s, respectively. Means, ranges and all ten measurements are
also retained. These costs include imports, process startup, validation
and compressed report I/O, but exclude reference enumeration and builds.
Cost measurements ran separately after the evaluation campaign; they are
not isolated IPC overhead or an equivalent-work speed comparison.

To reproduce the expansion from the configured repository environment, use new
output directories and run the cost command after the evaluation finishes:

```bash
python experiments/run_simulator_comparison.py \
  --profile experiments/standalone-profile.json \
  --output results/standalone-16offsets
python scripts/benchmark_simulators.py \
  --campaign results/standalone-16offsets --repeats 10 \
  --output results/standalone-cost-10repeats
```

Raw results remain local in those generated directories. They include all
1,530 evaluation reports, the separate calibration, paired observations,
source hashes and runtime measurements. Local validation confirmed the
existing 81 Python and 83 Q2NS native test results against their original
logs; no new regression-test count is implied by the expanded run count.

## Research question

For the same Swap-to-Teleport workflow, how do the execution models chosen in
Q2NS, NetSquid and QuCl change predicted completion time and final output state,
and what does each simulation cost to run?

The primary comparison concerns particular implementations and configurations.
It does not establish which simulator is universally more accurate. QuCl is
the detailed comparison model; independent timing and state checks remain its
correctness references.

## What the current source supports

The audited Q2NS base is `f22ba28f437099ba3cf9956ca332ba5ce8bb14fd`, with the
external-app integration patches shipped here. The processor and state-backend
files cited below are unchanged by those patches. NetSquid is version 1.1.7.

| Component | Finding | Consequence for the comparison |
|---|---|---|
| Q2NS gate timing | `QGate::Duration()` stores optional metadata. `QProcessor::Apply()` invokes the state update synchronously, and `MeasureBell()` calls CNOT, H and two measurements synchronously. This path does not schedule a completion or reserve a timed execution slot. | Specifying the same gate-duration numbers does not give the same timing. A difference can exist with a single workflow, before any processor contention. |
| Q2NS memory noise | The audited processor/state path has no built-in elapsed-storage-time T1/T2 update. A density-matrix backend alone does not provide that update. | Native Q2NS cannot be assumed to match QuCl's memory model by configuration. |
| Q2NS channel noise | QMaps include sampled Pauli dephasing/depolarization and custom callbacks. | Channel noise is not interchangeable with continuous memory aging or gate noise; equal probability values do not establish equal channels. |
| Q2NS teleport resource | The native `TeleportationApp::DoSourceStart()` creates its own input qubit and Bell pair. | The complete chained scenario needs an explicit handoff of the already-swapped A/B pair. Calling the unadapted example would simulate a different workflow. |
| NetSquid classical transport | `ClassicalChannel` supports fixed delay and configurable delay models. | Fixed delay is a deliberately selected baseline, not a fundamental restriction of NetSquid. |
| Current Fixed classical delay | [FixedChainParticipant](../experiments/chain_models.py) schedules virtual deliveries and is driven by the QuCl coordinator. | It is an ablation, not an independently implemented NetSquid-only execution. |

Pinned Q2NS sources:
[gate descriptor](https://github.com/QuantumInternet-it/q2ns/blob/f22ba28f437099ba3cf9956ca332ba5ce8bb14fd/model/q2ns-qgate.h),
[processor](https://github.com/QuantumInternet-it/q2ns/blob/f22ba28f437099ba3cf9956ca332ba5ce8bb14fd/model/q2ns-qprocessor.cc),
[density-matrix backend](https://github.com/QuantumInternet-it/q2ns/blob/f22ba28f437099ba3cf9956ca332ba5ce8bb14fd/model/q2ns-qstate-dm.cc),
[channel maps](https://github.com/QuantumInternet-it/q2ns/blob/f22ba28f437099ba3cf9956ca332ba5ce8bb14fd/model/q2ns-qmap.cc),
[teleportation application](https://github.com/QuantumInternet-it/q2ns/blob/f22ba28f437099ba3cf9956ca332ba5ce8bb14fd/model/q2ns-teleportation-app.cc).
For the component/event model, see the
[NetSquid paper](https://doi.org/10.1038/s42005-021-00647-8).

## Three actual execution paths

| Execution | Classical transport | Quantum execution | Required implementation |
|---|---|---|---|
| QuCl | Actual ns-3/Q2NS packets on A--R--B | NetSquid timed gates, shared R/A/B FIFO and memory noise | Existing complete-chain runner |
| NetSquid-only, calibrated classical delay | NetSquid classical channels with separately calibrated delays | The same quantum instruction/noise definitions and FIFO policy as QuCl | A standalone NetSquid event loop and protocols; no ns-3 process, IPC or QuCl cross-simulator event coordinator |
| Q2NS-only, native quantum execution | Actual ns-3/Q2NS packets on A--R--B | Q2NS density-matrix backend with its native timing and supported noise semantics | A native chained scenario with same-qubit handoff; no NetSquid process or delegated state calculation |

The Q2NS scenario may adapt application/resource wiring to accept existing
qubits and retain packet-dependent progression. Document that adaptation. Do
not silently add a timed processor, memory-noise engine or QuCl scheduler and
then present that implementation as unmodified Q2NS. Such extensions would be
separate, explicitly named comparisons.

Every runner must complete this causal chain:

```text
Elementary EPR delivery -> workflow start at R -> swapping at R
 -> swap result R--B -> B correction -> readiness B--R--A
 -> teleportation at A using the swapped pair
 -> teleport result A--R--B -> final B correction
```

NetSquid-only uses directed native `ClassicalChannel` components through the
same A/R/B nodes. Calibration measures the three complete message paths
separately, including both uses of R--B. The first hop of a two-hop path receives
its unloaded serialization-plus-propagation delay (capped by the supplied path
delay), and the second receives the remainder. This is an explicit fixed-delay
representation of each calibrated end-to-end path, not a measured per-hop queue.
The sum equals the supplied path delay exactly. Do not add propagation/serialization a second time to an
already end-to-end calibrated value. Use independent low-quantum-contention
calibration traffic phases, record calibration provenance, and freeze those
values before evaluation. The calibrated baseline therefore depends on an
external calibration; it does not predict packet congestion from first principles.

## Evaluation in three parts

### 1. Standalone comparison

Start with the common noiseless state model, a single workflow and no background
traffic. All three executions must recover each of the six input states, consume
the same swapped pair, and respect message/readiness dependencies. Verify
measurement conventions and outcome coverage separately for each backend; the
same integer RNG seed does not imply the same outcomes across simulators.

Do not require native Q2NS and QuCl latency to agree in this control: native Q2NS
has instantaneous quantum updates in the audited path. Report the difference in
operation durations explicitly, then assess the additional change as workflow
overlap and network load increase. Log packet-generation times so this difference
is not incorrectly attributed solely to queue waiting.

Next compare QuCl with NetSquid-only using the identical native quantum model,
including T1/T2 memory aging and gate noise. Their differences can be attributed
to the chosen classical transport representations once the quantum and protocol
equivalence controls pass. Do not interpret a noisy QuCl versus noise-free Q2NS
fidelity difference, or even its trend, as isolated evidence of execution coupling.
If a common noisy model cannot be established, restrict that matched noisy
comparison to QuCl and NetSquid-only and disclose the Q2NS capability boundary.

Primary observables are complete-workflow latency, final Bob fidelity relative
to the originally prepared input, processor waits where modeled, and each of the
three message-path delays. Report differences from QuCl as model differences;
an accuracy claim additionally requires the relevant independent reference.

### 2. Ablations

Retain No R contention and Decoupled timing on the entire Swap-to-Teleport chain.
Both already calculate a native final state. They provide the controlled
attribution that a comparison between unlike simulator models cannot provide.

Keep Fixed classical delay as an implementation control until the standalone
NetSquid runner reproduces its operation times and states under identical delay
constants and event-tie policy. After that test passes, the main presentation
can use NetSquid-only in its place. Preserve the old runner and datasets for
reproducibility; generate new standalone data and runtime measurements.

### 3. Simulation cost

Measure all runners from the same external launcher. Report end-to-end wall
time and wall time per completed workflow. Peak memory remains a future metric. Include setup
and process startup; either include Python imports for all cold-start timings or
report a separate, consistently delimited steady-process measurement. Exclude
builds, reference enumeration and plotting from the execution-cost measurement.
Keep normal trace collection and output-I/O policy consistent and state them.

Use the same finite traffic window and termination rule, including traffic drain
where packets are simulated. NetSquid-only does not execute the background packet
events represented by its calibrated delays; disclose that work difference.
QuCl raw reports retain coordination rounds. IPC counts are zero for the two
standalone modes; the current cost campaign does not instrument QuCl IPC. A runtime
ratio against a simpler or different state backend is total simulation cost,
not an isolated estimate of coordination overhead.

The initial cost pilot uses 1, 4, 8, 16 and 32 workflows, loads 0 and 0.75, and
three timed repetitions per condition, interleaving runner order on the same
host. Use at least ten repetitions for a subsequent cost characterization. The current
chain configuration supports at most 32 workflows; 64 requires an explicit
capacity change and validation. Report uncertainty over repetitions and keep
runtime repetitions distinct from independent traffic samples.

## Workload and statistics

Reuse the explicit parameters in [chain-profile.json](../experiments/chain-profile.json),
not the illustrative example defaults:

- Four complete workflows, six input states, and starts at 0.5x, 1x and 2x the
  27.405-us QuCl/NetSquid R BSM duration. Keep the resulting *absolute* start times
  identical in Q2NS; do not normalize by its zero native BSM duration.
- Loads 0, 0.25, 0.50 and 0.75 with identical packet sizes, paths and traffic phases
  wherever packet transport is executed. In calibrated NetSquid-only runs, load
  indexes the calibration condition rather than actual background packets.
- Sixteen traffic phases per loaded cell, independent calibration phases, and
  no fictitious extra samples for deterministic zero-load conditions.
- Identical resource creation, delivery and memory-placement times where the
  backend represents them. Do not replace the swapped pair or reset its age.

For state quality, calculate fidelity from each simulator's own final state.
The independent reference validates that state; it must not replace a missing
Q2NS state-evolution implementation. Specify whether the endpoint is a sampled
outcome or a branch-probability-weighted expectation. If outcomes are sampled,
choose and document sufficient quantum repetitions for each backend before the
main campaign. Do not pair raw outcomes solely by equal cross-library seeds.

Average workflows and input states within a traffic phase before resampling
traffic phases. Report paired differences and intervals where workload pairing
is meaningful. State whether quantum-outcome variability is sampled or exactly
enumerated, and keep calibration uncertainty conditional as in the existing
campaign. No general conclusion follows from an expected difference alone.

## Validation and interpretation

The dedicated [tests](../tests/test_standalone.py) check:

- Native Q2NS six-input, all-16-joint-outcome coverage, live density matrices,
  same-qubit handoff, readiness gating, and zero-time quantum operations.
- Native NetSquid channel events, exact path delays, and Fixed-Dc equivalence
  of operation schedules and final states, including zero-delay/equal-time events.
- Explicit rejection of unsupported Q2NS noise, packet overflow, altered
  physical-qubit provenance, and corrupted packet timing.
- A single-workflow control proving that Q2NS timing differs even without
  contention, by the omitted gate/measurement/correction service time.

The existing full release suite still runs on the default coupled path. The
native C++ option changes no Q2NS processor or state-backend source. Only the
scenario's request/completion callbacks select local Q2NS state operations.

## Files and commands

| Purpose | Source |
|---|---|
| Standalone NetSquid event protocol | [standalone_netsquid.py](../experiments/standalone_netsquid.py) |
| Standalone Q2NS configuration, output parsing and validation | [standalone_q2ns.py](../experiments/standalone_q2ns.py) |
| Shared actual Q2NS apps and packet topology | [cosim-chained.cc](../examples/cosim-chained.cc), `--nativeConfig` / `--nativeOutput` mode |
| Native Q2NS state operations and QChannel resource delivery | [q2ns-chain-backend.h](../examples/q2ns-chain-backend.h) |
| Calibration, paired campaign and cost runner | [run_simulator_comparison.py](../experiments/run_simulator_comparison.py) |

After the existing [installation](INSTALL.md), rebuild from the ns-3 root:

```bash
./ns3 build cosim-chained
```

From this repository root, using the NetSquid Python environment:

```bash
python experiments/run_simulator_comparison.py --pilot \
  --output results/standalone-pilot
python scripts/benchmark_simulators.py --campaign results/standalone-pilot \
  --repeats 3 --output results/standalone-cost
python -m unittest discover -s tests -p test_standalone.py -v
python scripts/run_tests.py
```

The pilot keeps all three start intervals and six states, but uses loads 0/0.75
and four evaluation traffic phases. It has five separate calibration runs and
450 evaluation runs: 270 across the three noise-off engines and 180 across the
two matched noisy engines. Each run completes four workflows. Zero load executes
once per interval/input; repeating its inactive traffic seed adds no diversity.
The dedicated cost pilot adds 90 fresh-process executions. It includes
engine-specific Python imports, engine startup, validation and report I/O,
excludes independent references and parent configuration preparation, and
measures different amounts of simulated work. The Q2NS worker asserts that
NetSquid was not imported. The optional `--runtime-repeats` path in the
campaign runner instead measures a shared Python launcher that imports all
engines; do not mix those measurements with this engine-specific benchmark. It is not isolated IPC overhead.
Three repeats are too few for a reliable cost confidence interval.

To use all loads and 16 evaluation phases in the existing profile, omit `--pilot`
and supply a fresh output directory. Existing directories are never overwritten.
Increase runtime repeats independently. These runners leave the older ablation
campaigns unchanged; No R contention and Decoupled remain separate controlled
experiments, not renamed standalone tools.

Every native Q2NS report stores its own density matrix and native fidelity. The
Python launcher uses the shared scenario parser (which imports NetSquid
constants), but the **standalone C++ executable performs all Q2NS simulation
without NetSquid or IPC**. NetSquid-only reuses QuCl's quantum programs, resource
management and FIFO policy, while its transport protocol advances entirely
inside `ns.sim_run()` and actual `ClassicalChannel` callbacks.

Noisy summaries use the exact 16-branch expectation from a separate reference
at each run's observed operation times, alongside native sampled fidelity and
checkpoint validation. Noise-off native Q2NS fidelity is sampled; all validated
branches recover the same ideal input. Equal integer seeds across Q2NS and
NetSquid do not imply identical branches. A three-way noisy fidelity comparison
is intentionally unsupported, rather than silently replacing Q2NS output with
a NetSquid calculation.

Plot the completed comparison with the optional matplotlib environment:

```bash
python scripts/plot_simulator_comparison.py results/standalone-pilot \
  --runtime results/standalone-cost --output results/standalone-pilot/figures
```

The default plot selects the 0.5x start interval; all three intervals remain in
`observations.csv` and `summary.json`. The two panels are complete-chain latency
(noise off, three engines) and final Bob fidelity (matched noisy model, two
engines). Fidelity uses an expanded axis and point markers. Captions record the
comparison boundaries. NetSquid-only has constant predictions within each
calibration condition; its collapsed phase interval is not evidence of zero
calibration uncertainty.

The standalone `ipc_messages=0` field describes simulation coordination, not
optional post-run reference-worker communication. Independent references are
computed after the simulation and are disabled for cost measurements.

## First verified pilot

The 2026-10-24 run passed **81 Python tests** (11 dedicated standalone tests) and
**83 native Q2NS test cases**. All 450 evaluation runs and five calibration runs
completed; maximum reference density-matrix discrepancy was `8.88e-16`. The
separate engine-specific cost campaign completed 90 executions. These counts
include a finite four-phase pilot, not the complete 16-phase performance grid.

At the 0.5x start interval and load 0.75, noise-off mean chain latency was
685.14 us (QuCl), 628.33 us (NetSquid-only) and 560.40 us (Q2NS-only). With the
matched noisy model, final branch-weighted six-input fidelity was 0.71411
(QuCl) and 0.71570 (NetSquid-only). Q2NS-only has no matched noisy fidelity
result. At zero load and the 1x interval, QuCl/NetSquid latency was 576.110 us
and Q2NS latency was 521.280 us: the 54.830-us difference is exactly two BSM
and two correction durations, before any processor waiting.

Generated artifacts remain local under `results/standalone-pilot/`,
`results/standalone-cost/` and `results/standalone-validation/`. The plotter
creates `figures/RESULTS.md`, captions and PDF/PNG figures. Source hashes, raw
reports, calibration values, observations and phase-bootstrap intervals are
retained for audit. The earlier ablation datasets are not overwritten.
