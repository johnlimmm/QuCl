"""Export NetSquid density matrices and squared fidelity at a checkpoint."""

from netsquid.qubits import ketstates, qubitapi as qapi


def encode_state(qubits, order, at, target=ketstates.b00):
    dm = qapi.reduced_dm(qubits)
    return dict(
        sim_time_ns=at,
        fidelity=float(qapi.fidelity(qubits, target, squared=True)),
        density_matrix=dict(qubit_order=order, real=dm.real.tolist(), imag=dm.imag.tolist()),
    )
