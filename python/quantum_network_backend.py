"""Actual NetSquid nodes, lossless channels, and memory-placement callbacks."""

import netsquid as ns
from netsquid.nodes import Network, Node
from netsquid.components import QuantumChannel
from netsquid.components.models.qerrormodels import DepolarNoiseModel
from netsquid.qubits import qubitapi as qapi
from quantum_state import encode_state


class QuantumNetworkBackend:
    def __init__(self, core):
        self.core = core
        self.pending = {}
        self.deliveries = []
        self.network = Network("QuCl-QuantumNetwork")
        self.nodes = {
            name: Node(name, ID=i, qmemory=core.devices[name])
            for i, name in enumerate(("A", "R", "B"))
        }
        self.network.add_nodes(list(self.nodes.values()))
        self.ports = {}
        self.channels = {}
        for name, destination in (("RA", "A"), ("RB", "B")):
            cfg = core.config["quantum_links"][name]
            channel = QuantumChannel(
                "QuantumChannel-" + name,
                delay=cfg["delay_ns"],
                models={
                    "quantum_noise_model": DepolarNoiseModel(
                        depolar_rate=cfg["depolar_rate_hz"], time_independent=False
                    )
                },
            )
            source_port, dest_port = self.network.add_connection(
                self.nodes["R"],
                self.nodes[destination],
                channel_to=channel,
                label=name,
                port_name_node1="out-" + name,
                port_name_node2="in-" + name,
            )
            self.ports[name] = self.nodes["R"].ports[source_port]
            self.channels[name] = channel
            self.nodes[destination].ports[dest_port].bind_input_handler(
                lambda message, d=destination: self.receive(d, message)
            )

    def pair(self, adapter, handle, remote, local):
        c = self.core
        a, b = qapi.create_qubits(2)
        qapi.operate(a, ns.H)
        qapi.operate([a, b], ns.CNOT)
        c.devices["R"].put(b, positions=local[1])
        c.register(adapter.sid, handle, "pair", [remote, local], state="PROVISIONING")
        name = "RA" if remote[0] == "A" else "RB"
        cause = c.emit(
            "EPR_CREATED",
            adapter.sid,
            c.roots[adapter.sid],
            resource_handle=handle,
            created_ns=c.now_ns,
            source_node="R",
            local_location=local,
            remote_destination=remote,
        )
        cause = c.emit(
            "QCHANNEL_SEND",
            adapter.sid,
            cause,
            resource_handle=handle,
            channel=name,
            send_ns=c.now_ns,
        )
        self.pending[id(a)] = dict(
            adapter=adapter,
            handle=handle,
            qubit=a,
            local=local,
            remote=remote,
            channel=name,
            cause=cause,
        )
        self.ports[name].tx_output(a)

    def receive(self, destination, message):
        c = self.core
        c.sync_time()
        c.phase = "PROVISIONING"
        if message is None:
            raise RuntimeError("empty quantum receive callback")
        for qubit in message.items:
            record = self.pending.pop(id(qubit), None)
            if (
                record is None
                or qubit is None
                or record["qubit"] is not qubit
                or record["remote"][0] != destination
            ):
                raise RuntimeError("unknown, duplicate, or lost quantum delivery")
            adapter = record["adapter"]
            handle = record["handle"]
            resource = c.resources[(adapter.sid, handle)]
            if resource["state"] != "PROVISIONING":
                raise RuntimeError("delivery of non-provisioning resource")
            position = record["remote"][1]
            if c.devices[destination].peek(position)[0] is not None:
                raise RuntimeError("destination memory occupied")
            c.devices[destination].put(qubit, positions=position)
            cause = c.emit(
                "QCHANNEL_DELIVERED",
                adapter.sid,
                record["cause"],
                resource_handle=handle,
                channel=record["channel"],
                delivery_ns=c.now_ns,
                location=record["remote"],
            )
            resource.update(state="AVAILABLE", ready_ns=c.now_ns)
            cause = c.emit(
                "RESOURCE_AVAILABLE", adapter.sid, cause, resource_handle=handle, ready_ns=c.now_ns
            )
            order = (
                [record["remote"], record["local"]]
                if destination == "A"
                else [record["local"], record["remote"]]
            )
            state = encode_state(
                [c.peek(loc) for loc in order],
                ["A", "R_AR"] if destination == "A" else ["R_RB", "B"],
                c.now_ns,
                ns.qubits.ketstates.b00,
            )
            self.deliveries.append(
                dict(
                    session_id=adapter.sid,
                    resource_handle=handle,
                    channel=record["channel"],
                    send_ns=0,
                    delivery_ns=c.now_ns,
                    state=state,
                    placement_source="quantum_channel_port",
                )
            )
            adapter.resource_available(cause)

    def snapshot(self):
        if self.pending:
            raise RuntimeError("undelivered quantum resources")
        return dict(
            nodes={
                name: dict(
                    node_id=node.ID, memory=node.qmemory.name, positions=node.qmemory.num_positions
                )
                for name, node in self.nodes.items()
            },
            channels={
                name: dict(
                    source="R",
                    destination="A" if name == "RA" else "B",
                    component=type(channel).__name__,
                    **self.core.config["quantum_links"][name],
                )
                for name, channel in self.channels.items()
            },
            deliveries=self.deliveries,
            ready_notification="ideal_zero_delay",
            loss_model="none",
            transit_memory_noise=False,
        )
