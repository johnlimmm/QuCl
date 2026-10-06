# Architecture

QuCl performs co-simulation of ns-3/Q2NS and NetSquid. The problem it addresses
is execution coupling: packet arrivals, resource availability and quantum
completions determine when subsequent operations can execute. The mechanism
that preserves these dependencies is cross-simulator event coordination.

## Ownership and process boundary

The Python process owns NetSquid's simulation, quantum states, memory positions,
resource reservations and processor admission queues. The ns-3 child process
owns Q2NS protocol applications, classical sockets and packet/device queues.
They exchange logical requests, measurement bits, resource-ready notifications
and completion events over a bounded line-oriented Unix socket.

The socket is an implementation channel. Its wall-clock latency never appears
as simulated network propagation or serialization. Fidelity and density matrices
are diagnostic outputs in the Python process, not classical protocol payloads.

## Native instruction execution

[HybridExecutionCore](../python/execution_core.py) provides the session/request
registry, ownership checks and FIFO data structures.
[NativeExecutionCore](../python/native_core.py) submits real
[QuantumPrograms](../python/native_programs.py) to NetSquid QuantumProcessors.
A BSM contains CNOT, H and two sequential measurements. Correction contains
X-or-identity and Z-or-identity slots. Program-completion callbacks release the
processor and return results to the correct Q2NS application.

No operation is completed solely because a predicted end timestamp has elapsed.
Instruction start/completion checkpoints are diagnostic observations inserted
through a native zero-duration instruction. They do not implement gate dynamics.

Memory T1/T2 noise is attached to memory positions. Physical instructions also
apply the same storage-noise model over their active duration because active
instruction time is excluded from idle-position memory noise by the tested
NetSquid processor. Optional gate depolarization is added per operand before the
ideal gate action, after active-duration storage noise. Identity slots and
measurements receive storage noise but no additional gate depolarization.
The independent reference implements this noise convention separately.

## Cross-simulator event coordination

[CrossSimulatorCoordinator](../python/native_core.py) schedules the next ns-3 event
time as a wakeup on the NetSquid event calendar. Native gate and channel events
execute on that calendar. A completion schedules a same-time coordination wakeup,
so its classical consequence cannot be injected after ns-3 has advanced past it.
The [C++ bridge](../model/cosim-bridge.h) uses the public ns-3 simulator hook and
scheduler interface to execute bounded event batches at the requested time.

At a boundary, completed programs and resource-ready notifications are delivered
to ns-3. Packet/application events generate requests, which are admitted to the
appropriate quantum FIFO. Additional work at the same timestamp is processed
before advancing. Integer-nanosecond checks, ordered event IDs and causal links
are enforced in the reports.

[ProvisionedCoordinator](../python/provisioned_core.py) adds resource-ready
notifications to this coordination. [ChainedCoordinator](../python/chained_core.py)
uses the same implementation for the Swap-to-Teleport workflow; teleportation
readiness still arrives through the actual ns-3 packet path.

Reports that identify the implementation use the `coordinator` field.
The event-boundary trace remains `bridge_steps`. Optional runtime profiles use
`coordination_seconds` and `coordination_seconds_per_transaction` for the time
spent executing the coordinated event loop.

For independent sessions, already-ready starts at a timestamp precede readiness
notifications delivered at that timestamp. Within each group, configuration
order resolves ties. Queue admission is based on resource eligibility; an earlier
session with missing resources does not block a later eligible session.

## Resource lifecycle and protocol adapters

Resource keys are `(session_id, logical_handle)`. Physical memory positions are
also checked to prevent two active reservations from overlapping even if their
logical handles differ. A typical input moves from in-flight delivery to
available, reserved, in use and consumed. BSM creates an output with a pending
Pauli frame; correction makes it usable.

[QuantumNetworkBackend](../python/quantum_network_backend.py) creates the native
nodes and lossless quantum channels. Actual channel-port arrivals place remote
EPR halves into destination memories. The quantum-resource manager supplies
ideal zero-delay knowledge of initial delivery readiness to Q2NS; a heralding
protocol is not modeled.

In the [chained core](../python/chained_core.py), Swap's A/B output is handed to
Teleport under a new logical owner while retaining the same physical qubits.
Teleport's input already exists in A memory and ages while waiting. The real
B→R→A readiness packet gates A's BSM request. Swap and Teleport corrections share
one B FIFO; concurrent workflows also share R and A FIFOs.

The [independent-session core](../python/provisioned_core.py) uses R as the source
for both protocols. It exercises cross-protocol sharing of R/B without the
Swap-to-Teleport resource dependency. This is a separate example mode, not the
main workflow's placement.

## Packet timing

Q2NS builds its native result payloads. Swap uses its existing control header
plus configured padding; Teleport sends its existing two measurement bytes.
Readiness and finite background flows use a small UDP helper. Packet tags attach
observation IDs without changing wire size.

Actual device enqueue/dequeue, PHY and application events are recorded. Default
queue disciplines are removed so waiting is attributed to the device FIFO.
Independent FIFO calculations and timestamp decompositions check serialization,
propagation, processor waiting and complete workflow latency. Every normal run
requires zero packet drops; overflow is a negative test.

## State validation

Live output fidelity comes from NetSquid's `qubitapi.fidelity` with
`squared=True`; density matrices come from `qubitapi.reduced_dm`. No analytic
fidelity-decay formula substitutes for the native state.

[Independent reference circuits](../python/reference_chained.py) run in isolated
processes and accept physical parameters plus observed operation times. They do
not import the production scheduler, adapters, programs or noise helpers. They
compare sampled branch states at instruction and protocol checkpoints. Optional
joint-branch enumeration computes a distinct probability-weighted expectation.
Independent timing/packet oracles complement these state checks: supplying
observed times to a reference does not itself verify those times.

The [tests](../tests/) cover noise-free and noisy states, six input states,
measurement outcomes, channel delivery, FIFO contention, qubit continuity,
no duplicate quantum ownership, and failures such as corrupt traces or repeated
readiness. See [Examples](EXAMPLES.md#run-tests) for commands.
