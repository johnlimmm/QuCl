# Configuration

Scenarios are JSON objects. Unknown fields and invalid values are rejected.
Times and durations are integer **nanoseconds**; data rates are **bits/second**.
One microsecond is 1,000 ns; one millisecond is 1,000,000 ns.

## Common fields

| Field | Default | Meaning |
|---|---|---|
| `name` | `hybrid-direct-start` | Descriptive label |
| `seed` | `7` | NetSquid random seed, 0 through 2³²−1 |
| `result_link` | `{ "rate_bps": 10000000, "delay_ns": 200000 }` | Classical R–B link |
| `result_payload_bytes` | `80` | Swap result UDP payload, 60–1400 bytes |
| `queue_packets` | `128` | Device FIFO packet limit |
| `memory_noise` | `{ "model": "T1T2NoiseModel", "T1_ns": 20000000, "T2_ns": 10000000 }` | Native memory model |
| `gate_depolar_probability` | `0.0` | Per-operand depolarization before CNOT/H/X/Z |
| `background` | No packets | Optional finite R→B background flow |
| `quantum_links` | See below | Lossless initial EPR-half delivery |
| `native_instructions` | See below | Native physical instruction durations |

With finite T1, this implementation accepts T2 ≤ T1, matching the tested NetSquid
model's supported setting. Setting both T1 and T2 to zero disables memory noise.
That alone does not disable gate or channel noise if those were enabled separately.

### Native operations

```json
{
  "native_instructions": {
    "cnot_ns": 600000,
    "h_ns": 200000,
    "measure_ns": 400000,
    "x_ns": 250000,
    "z_ns": 250000
  }
}
```

Partial instruction objects override individual defaults. BSM executes CNOT, H,
then two serial measurements: its default duration is 1,600,000 ns. Correction
has one X slot and one Z slot: its default duration is 500,000 ns. If a measurement
bit is zero, an identity operation occupies the corresponding slot. This keeps
correction duration fixed across branches while allowing branch-dependent states.

`bsm_duration_ns` and `correction_duration_ns`, if explicitly supplied, must equal
these sums. Set individual instruction durations to change execution timing.
Defaults are illustrative; NetSquid provides configurable instruction semantics,
not universally prescribed hardware timings.

### Quantum delivery

```json
{
  "quantum_links": {
    "RA": { "delay_ns": 800000, "depolar_rate_hz": 0 },
    "RB": { "delay_ns": 1200000, "depolar_rate_hz": 0 }
  }
}
```

A source at R prepares ideal EPRs at time zero, keeps local halves in R memory,
and sends remote halves through native NetSquid channels. Native channel
noise can act during transit; destination-memory noise starts upon placement.
Inputs/EPRs are not regenerated. Loss, retries and dynamic generation are unsupported.
Link fields can be partially overridden. Standalone R/B teleport uses only RB.

### Background traffic

```json
{
  "background": {
    "result": {
      "start_ns": 2000000,
      "interval_ns": 900000,
      "count": 20,
      "payload_bytes": 1000
    }
  }
}
```

The flow is periodic, finite, and uses a separate UDP port. It shares the R→B
transmit queue with result/readiness traffic traversing that direction, but never
creates a quantum request. `count=0` disables it. The default flow settings are
start 0, interval 1,000,000 ns, count 0, payload 1000 bytes. Count is limited to 2000.

Wire serialization includes 30 bytes beyond UDP payload: PPP (2), IPv4 (20), UDP
(8). Observation packet tags add no wire bytes. Quantum gates and packet
propagation are separate delays; IPC wall time is never added to simulation time.

## Chained workflows

Use `chains`, not `sessions`, with the chained runner:

```json
{
  "chains": [
    { "chain_id": 1, "session_start_ns": 2000000, "input_state": "+i" },
    { "chain_id": 2, "session_start_ns": 2800000, "input_state": "-i" }
  ],
  "access_link": { "rate_bps": 10000000, "delay_ns": 200000 }
}
```

There must be 1–32 unique positive chain IDs. States are `0`, `1`, `+`, `-`, `+i`,
and `-i`. The runner allocates separate Swap/Teleport session IDs for each chain.
The input is prepared at A at time zero. `session_start_ns` activates swapping at
R once its resources are ready. Teleportation at A starts after the swapped pair
is usable and the readiness packet arrives; it does not start at this timestamp.

`access_link` is the classical A–R link. It defaults to 10 Mb/s and 200,000 ns.
Readiness travels B→R→A; teleport measurement results travel A→R→B. The quantum
A–B pair is handed over in place without replacing either physical qubit.

## Independent sessions

Use `sessions` with the provisioned runner:

```json
{
  "sessions": [
    { "session_id": 1, "protocol": "swap", "session_start_ns": 2000000 },
    { "session_id": 2, "protocol": "teleport", "session_start_ns": 2000000,
      "input_state": "+i" }
  ]
}
```

There must be 1–64 unique positive session IDs. Both protocols execute their BSM
at R and correction at B. Each has distinct quantum resources, even when its
local handle names match another session's. Explicit `epr_created_ns` and, for
teleport, `input_created_ns` are accepted only as zero.

## Bounds and ordering

All timestamps must be nonnegative integers no greater than 2⁵³−1 ns. Positive
gate durations, nonzero serialization and finite channel/noise values are
required. Configurations producing exact half-nanosecond serialization ties are
rejected. Queue admission occurs when both the session and its resources are
ready. Equal-time ordering is deterministic and checked against packet/FIFO
oracles; see [Architecture](ARCHITECTURE.md).
