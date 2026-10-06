# QuCl

QuCl connects **ns-3 and Q2NS classical protocol execution** to **NetSquid quantum
state evolution**. Real packet arrivals and native quantum program completions
jointly determine when the next operation can execute.

QuCl is a co-simulation framework that addresses **execution coupling** through
**cross-simulator event coordination**.

The main example performs **entanglement swapping at R, followed by teleportation
from A to B using the resulting A–B pair**. NetSquid executes timed gates, memory
noise and quantum-channel delivery. Q2NS retains its protocol applications and
classical UDP packet paths.

## Source code

The **ns-3 C++ simulation code is in [examples/](examples/)**. Each Python runner
starts its compiled ns-3 participant and coordinates it with NetSquid. Paths in
the tables below are relative to this repository's root.

| Execution mode | ns-3 / Q2NS C++ source | Python entry point |
|---|---|---|
| Swap → A/B Teleport workflow | [cosim-chained.cc](examples/cosim-chained.cc) | [run_chained.py](python/run_chained.py) |
| Independent Swap / R/B Teleport sessions | [cosim-provisioned.cc](examples/cosim-provisioned.cc) | [run_provisioned.py](python/run_provisioned.py) |

| Component | Location and role |
|---|---|
| ns-3 event stepping and IPC | [cosim-bridge.h](model/cosim-bridge.h) advances ns-3 at synchronization boundaries and exchanges messages with Python. |
| Readiness and background UDP traffic | [cosim-agent-app.cc](model/cosim-agent-app.cc) provides the UDP helper; protocol result packets use Q2NS applications. |
| Shared quantum execution | [execution_core.py](python/execution_core.py) manages requests and resource ownership; [native_core.py](python/native_core.py) runs the shared FIFOs, native completion callbacks and cross-simulator event coordination. |
| NetSquid gates and quantum channels | [native_programs.py](python/native_programs.py) defines timed BSM/correction programs; [quantum_network_backend.py](python/quantum_network_backend.py) handles EPR-half delivery and memory placement. |
| Swap-to-Teleport handoff | [chained_core.py](python/chained_core.py) transfers the actual swapped A/B qubits to the teleport session. |
| Q2NS application changes | [SwapApp patch](integrations/q2ns-swap-external.patch) and [TeleportationApp patch](integrations/q2ns-teleport-external.patch), installed by [apply_q2ns_patches.py](scripts/apply_q2ns_patches.py). |
| Build definitions | [CMakeLists.txt](CMakeLists.txt) registers the module; [examples/CMakeLists.txt](examples/CMakeLists.txt) registers both C++ executables. |
| Inputs, experiments and tests | [scenarios/](scenarios/) contains example inputs; [experiments/](experiments/) contains the full comparison campaign; [run_sweep.py](scripts/run_sweep.py) runs a small delay sweep; [tests/](tests/) contains validation tests. |

This repository is installed as `contrib/cosim` inside a separate ns-3 checkout.
The **`./ns3` build script belongs to that parent checkout**; it is supplied by
ns-3 itself. The installed layout is:

```text
ns-3.47/                       # Run ./ns3 commands here
├── ns3                        # Upstream ns-3 build/run script
└── contrib/
    ├── q2ns/                  # Separately installed Q2NS, then patched
    └── cosim/                 # This QuCl repository
        ├── examples/          # ns-3 C++ simulation sources
        ├── model/             # ns-3 bridge and UDP helper
        ├── python/            # Runners and NetSquid execution
        └── scenarios/         # JSON example inputs
```

ns-3, Q2NS and NetSquid distributions are installed separately and are not bundled.
See [Installation](docs/INSTALL.md) for the complete setup commands.

## Execution flow

```text
A–R and R–B EPR delivery
    → local session start at R + resources ready
    → shared R FIFO → swapping BSM
    → Q2NS result packet R → B → shared B FIFO → correction
    → readiness packet B → R → A
    → shared A FIFO → teleportation BSM using the same A–B qubits
    → Q2NS result packet A → R → B → shared B FIFO → final correction
```

There is no separate controller node or synthetic command packet. Quantum state
exists only in NetSquid; Q2NS uses logical resource handles. Fidelity is computed
from the live NetSquid state with `qubitapi.fidelity(..., squared=True)`.

## Start here

1. Follow [Installation](docs/INSTALL.md) to install the pinned dependencies and
   apply the Q2NS external-execution patches.
2. Run [Examples](docs/EXAMPLES.md), including a noiseless six-state example,
   competing workflows and a small configurable delay sweep.
3. Read [Configuration](docs/CONFIGURATION.md) and
   [Architecture](docs/ARCHITECTURE.md) before extending the model.
4. Follow [Complete-workflow experiments](docs/CHAIN-EVALUATION.md) for the literature-based
   profile, six-input workload, paired model comparisons, statistics and Figures 3–5.

Once installed, run from the ns-3 root:

```bash
./ns3 build cosim-chained cosim-provisioned
"$QUCL_PYTHON" contrib/cosim/python/run_chained.py \
  contrib/cosim/scenarios/chain.json --output results/chain.json
```

`QUCL_PYTHON` is the absolute path to the Python interpreter with NetSquid
installed. The installation guide explains why this can differ from the Python
interpreter used by the ns-3 build wrapper.

## Included examples

| Scenario | Runner | Purpose |
|---|---|---|
| [chain](scenarios/chain.json) | [run_chained.py](python/run_chained.py) | One Swap → A/B Teleport workflow |
| [chain-noiseless](scenarios/chain-noiseless.json) | [run_chained.py](python/run_chained.py) | All six input states with noise disabled |
| [chain-contention](scenarios/chain-contention.json) | [run_chained.py](python/run_chained.py) | Four workflows with quantum and packet queues |
| [chain-gate-noise](scenarios/chain-gate-noise.json) | [run_chained.py](python/run_chained.py) | Optional gate depolarization and native memory aging |
| [swap](scenarios/swap.json) | [run_provisioned.py](python/run_provisioned.py) | Standalone A–R–B swapping |
| [mixed](scenarios/mixed.json) | [run_provisioned.py](python/run_provisioned.py) | Independent Swap and R/B Teleport sessions sharing R/B processors |

The last example intentionally places the independent teleport source at R to
exercise cross-protocol R contention. In the main chained workflow the teleport
source is A, and its EPR is the actual output of swapping.

## Validation

Both runners check event causality, resource ownership, native instruction
timing and classical packet timing. Independent NetSquid-only circuits validate
quantum states at matching operation times. The test suite also covers all six
input states, measurement branches, FIFO contention and rejected invalid runs.
The current release check passed **70 Python tests and 83 Q2NS native test cases**
(across eight native suites) in a separate ns-3.47 build. This includes the original
37 Python tests, 15 swapping-evaluation tests and 18 complete-chain evaluation
tests for model equivalence, final-state errors, downstream contention,
traffic-phase statistics and independent worker execution. Campaign execution
counts are separate from test counts.
See [testing instructions](docs/EXAMPLES.md#run-tests).

## Reproduce the comparison campaign

The [experiment profile](experiments/chain-profile.json) defines a 27.405 μs native BSM,
100 Mb/s classical links, three workflow-start intervals and four background
loads. These explicitly override the illustrative example defaults.

```bash
"$QUCL_PYTHON" contrib/cosim/experiments/run_chain_evaluation.py \
  contrib/cosim/experiments/chain-profile.json --workers 1 \
  --output-dir results/qucl-evaluation
```

This evaluates complete Swap → Teleport chains for six input states and compares
Full Sync with No R contention, Fixed classical delay and Decoupled timing over
the entire workflow, including final Bob output. It records sampled native fidelity separately from the exact
branch-weighted expectation. A separate renderer verifies the reports and produces
Figures 3–5 with traffic-phase bootstrap intervals. See the
[reproduction guide](docs/CHAIN-EVALUATION.md) for plotting dependencies, commands,
parameter sources and interpretation. Manuscripts and generated datasets are not bundled.

## Scope

This implementation uses a fixed A/R/B topology, IPv4/UDP, pre-created input/EPR
resources, lossless quantum channels with optional depolarization, serial native
BSM/correction instructions, and T1/T2 memory noise. Default numerical parameters
are illustrative simulation settings, not a calibrated hardware model. Dynamic
EPR generation, channel loss/retries and arbitrary topology configuration are
not implemented.

## License

QuCl is distributed under [GPL-2.0-only](LICENSE). Dependency licenses and retained
upstream notices are described in [Third-party notices](THIRD_PARTY_NOTICES.md).
