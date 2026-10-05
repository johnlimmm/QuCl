# Examples and experiments

Run these commands from the ns-3 root after [installation](INSTALL.md).
`QUCL_PYTHON` must point to the Python environment containing NetSquid.
The runners execute ns-3 themselves; do not launch their C++ participants separately.

## One complete Swap → Teleport workflow

```bash
"$QUCL_PYTHON" contrib/cosim/python/run_chained.py \
  contrib/cosim/scenarios/chain.json --output results/chain.json
```

This prepares two EPRs at R, delivers their remote halves to A and B, performs
swapping at R, and transports its measurement bits to B. After B corrects the
pair, a readiness packet reaches A. A teleports its input through that same A–B
pair. B corrects the received qubit only after the teleport result packet arrives.

The report contains:

| Field | Meaning |
|---|---|
| `config` | Fully expanded input parameters, seed and durations |
| `metrics.chains` | Per-workflow latency, completion time, waits and final fidelity |
| `events` / `ns3_events` | Causally linked quantum and packet events |
| `native_instructions` | Native gate start/end times and state checkpoints |
| `snapshot` | Resources, requests, channel deliveries and final states |
| `q2ns_status` | Classical protocol completion and zero Q2NS quantum-state ownership |
| `validation` | Timing, packet, ownership and resource-lifecycle checks |
| `cross_validation` | Independent NetSquid reference comparison |

`latency_ns` is final correction completion minus local session start.
`output_fidelity` is NetSquid's squared fidelity against the originally prepared
input for the sampled measurement branch. It includes input-memory aging.
It is not an average across traffic seeds or measurement branches.

## Noiseless six-state check

```bash
"$QUCL_PYTHON" contrib/cosim/python/run_chained.py \
  contrib/cosim/scenarios/chain-noiseless.json \
  --output results/chain-noiseless.json --enumerate-branches
```

The six workflows use `0`, `1`, `+`, `-`, `+i`, and `-i`. Memory, gate and channel
noise are disabled. Each final state should match its original input to numerical
precision. The optional flag evaluates all 16 joint Swap/Teleport measurement
branches in the independent reference. Its probability-weighted expected fidelity
is stored separately from the sampled native output. No branch average replaces
the live NetSquid fidelity in `metrics.chains`.

## Competing workflows and gate noise

```bash
"$QUCL_PYTHON" contrib/cosim/python/run_chained.py \
  contrib/cosim/scenarios/chain-contention.json --output results/chain-contention.json
"$QUCL_PYTHON" contrib/cosim/python/run_chained.py \
  contrib/cosim/scenarios/chain-gate-noise.json --output results/chain-gate-noise.json
```

The contention example shares R, A and B processors across four workflows and
adds finite background traffic on R→B. Inspect `metrics.chains[*].delays` to
separate resource readiness, processor waiting and classical transport.
The gate-noise example enables per-operand depolarization in addition to native
T1/T2 memory aging. These inputs demonstrate model configuration; they do not
calibrate a particular quantum device.

## Independent protocol sessions

```bash
"$QUCL_PYTHON" contrib/cosim/python/run_provisioned.py \
  contrib/cosim/scenarios/swap.json --output results/swap.json
"$QUCL_PYTHON" contrib/cosim/python/run_provisioned.py \
  contrib/cosim/scenarios/mixed.json --output results/mixed.json
```

In this diagnostic mode, Swap and independent Teleport sessions start at R and
share R/B processor FIFOs. Teleport's input and local EPR half are at R; its remote
half is at B. The sessions do not consume one another's output. Use the chained
runner above for swapping-assisted A→B teleportation.

## Run a small experiment

The sweep tool varies only R–B classical propagation delay, holding each input
scenario's start times, traffic schedule, gate times and quantum links fixed.

```bash
"$QUCL_PYTHON" contrib/cosim/scripts/run_sweep.py \
  contrib/cosim/scenarios/chain-contention.json \
  --delays-ns 100000 500000 1000000 --seeds 7 11 \
  --output-dir results/delay-sweep
```

This produces six compressed reports, `summary.csv` with one row per workflow,
and `complete.json` only after every run succeeds. Existing output directories
are rejected to prevent accidentally mixing experiments. Failed validated runs
retain their report and a `failure.json` marker.

Seeds control quantum measurements. The traffic schedule in these examples is
fixed; changing the quantum seed does not create an independent traffic phase.
For a traffic experiment, change `background.result.start_ns` or other traffic
inputs explicitly and save each configuration. Multiple sessions in a batch
share queues and are not independent statistical samples. Compare final-state
quality and latency within a stated workload; a later request also has older
resources because all inputs/EPRs are created at time zero.

## Run tests

Build both participants, `q2ns-test` and `test-runner` first:

```bash
"$QUCL_PYTHON" contrib/cosim/scripts/run_tests.py
```

This runs the Python integration suite and all available `q2ns-*` native suites,
including asynchronous SwapApp and TeleportationApp tests. Tests cover independent
reference states, six input states and branch coverage, same-time event ordering,
shared FIFO/resource isolation, physical-qubit handoff, and rejected corrupt
traces, duplicate readiness and packet overflow.

Use `--python-only` or `--native-only` on the test script to run one side.

For just one Python suite:

```bash
"$QUCL_PYTHON" -m unittest discover -s contrib/cosim/tests -p test_chained.py -v
```

Test reports go under `contrib/cosim/results/tests/` and are excluded from Git.
Intentional negative tests may print participant errors before reporting `ok`.
Runtime and output size depend on branch enumeration and reference checks.

## Troubleshooting

- **Participant not found:** build the named target; check `QUCL_NS3_ROOT` if using
  an external checkout or more than one ns-3 installation.
- **NetSquid import fails:** run with `QUCL_PYTHON`, not the system build interpreter.
- **Handshake/readiness errors:** recheck the Q2NS patches and rebuild participants.
- **Packet drop rejected:** increase `queue_packets` or reduce the offered traffic.
  Drops are rejected by the current correctness model, not silently discarded.
- **Unsupported configuration:** see the [accepted fields](CONFIGURATION.md).
  Changing a gate duration can also change the derived operation duration.
