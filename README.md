# QuCl

QuCl connects **ns-3 and Q2NS classical protocol execution** to **NetSquid quantum
state evolution**. Real packet arrivals and native quantum program completions
jointly determine when the next operation can execute.

The main example performs **entanglement swapping at R, followed by teleportation
from A to B using the resulting A–B pair**. NetSquid executes timed gates, memory
noise and quantum-channel delivery. Q2NS retains its protocol applications and
classical UDP packet paths.

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
| [chain](scenarios/chain.json) | `run_chained.py` | One Swap → A/B Teleport workflow |
| [chain-noiseless](scenarios/chain-noiseless.json) | `run_chained.py` | All six input states with noise disabled |
| [chain-contention](scenarios/chain-contention.json) | `run_chained.py` | Four workflows with quantum and packet queues |
| [chain-gate-noise](scenarios/chain-gate-noise.json) | `run_chained.py` | Optional gate depolarization and native memory aging |
| [swap](scenarios/swap.json) | `run_provisioned.py` | Standalone A–R–B swapping |
| [mixed](scenarios/mixed.json) | `run_provisioned.py` | Independent Swap and R/B Teleport sessions sharing R/B processors |

The last example intentionally places the independent teleport source at R to
exercise cross-protocol R contention. In the main chained workflow the teleport
source is A, and its EPR is the actual output of swapping.

## Validation

Both runners check event causality, resource ownership, native instruction
timing and classical packet timing. Independent NetSquid-only circuits validate
quantum states at matching operation times. The test suite also covers all six
input states, measurement branches, FIFO contention and rejected invalid runs.
The release check passed **37 Python tests and 83 Q2NS native test cases**
(across eight native suites) in a separate ns-3.47 build.
See [testing instructions](docs/EXAMPLES.md#run-tests).

## Scope

This implementation uses a fixed A/R/B topology, IPv4/UDP, pre-created input/EPR
resources, lossless quantum channels with optional depolarization, serial native
BSM/correction instructions, and T1/T2 memory noise. Default numerical parameters
are illustrative simulation settings, not a calibrated hardware model. Dynamic
EPR generation, channel loss/retries and arbitrary topology configuration are
not implemented.

## Repository layout

- [python/](python/): execution core, protocol adapters, runners and independent references.
- [model/](model/) and [examples/](examples/): ns-3 bridge and Q2NS participants.
- [integrations/](integrations/): patches against the pinned Q2NS source.
- [scenarios/](scenarios/) and [scripts/](scripts/): runnable inputs and setup/experiment tools.
- [tests/](tests/): executable validation tests; outputs are generated locally.

This is an ns-3 add-on, installed as `contrib/cosim`. Dependencies are installed
separately; ns-3, Q2NS and NetSquid distributions are not bundled.

## License

QuCl is distributed under [GPL-2.0-only](LICENSE). Dependency licenses and retained
upstream notices are described in [Third-party notices](THIRD_PARTY_NOTICES.md).
