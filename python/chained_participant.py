"""Native Q2NS participant with explicit ideal readiness notifications."""

import socket
import subprocess
from participant import Q2nsParticipant


class ChainedParticipant(Q2nsParticipant):
    """Configure the participant and share packet tracing and socket lifecycle helpers."""

    def __init__(self, binary, config, roots):
        self.process = None
        self.stream = None
        self.sock, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(15)
        try:
            self.process = subprocess.Popen(
                [str(binary), "--bridgeFd=" + str(peer.fileno())],
                pass_fds=(peer.fileno(),),
                stdout=subprocess.DEVNULL,
            )
        except BaseException:
            self.sock.close()
            raise
        finally:
            peer.close()
        self.stream = self.sock.makefile("rwb", buffering=0)
        self.source_sequence = 0
        try:
            if self.read() != ["HELLO", "COSIM_CHAINED", "5"]:
                raise RuntimeError("invalid hybrid handshake")
            c = config
            self.send(
                "CONFIG "
                + " ".join(
                    map(
                        str,
                        [
                            c["result_link"]["rate_bps"],
                            c["result_link"]["delay_ns"],
                            c["result_payload_bytes"],
                            c["queue_packets"],
                            len(c["sessions"]),
                            c["access_link"]["rate_bps"],
                            c["access_link"]["delay_ns"],
                        ],
                    )
                )
            )
            for s in c["sessions"]:
                self.send(
                    "SESSION {} {} {} {} {}".format(
                        s["session_id"],
                        s["session_start_ns"],
                        roots[s["session_id"]],
                        int(s["protocol"] == "teleport"),
                        s["parent_session_id"],
                    )
                )
            for side in ("result",):
                f = c["background"][side]
                self.send(
                    "FLOW {} {} {} {} {}".format(
                        side, f["start_ns"], f["interval_ns"], f["count"], f["payload_bytes"]
                    )
                )
            topology = self.read()
            if topology != ["NODES", "3", "0", "1", "2"]:
                raise RuntimeError("invalid native topology")
            self.nodes = dict(zip(("A", "R", "B"), map(int, topology[2:])))
            row = self.read()
            if len(row) != 3 or row[:2] != ["READY", "0"]:
                raise RuntimeError("invalid READY")
            self.now_ns = 0
            self.next_time = self._time(row[2])
        except BaseException:
            self.close()
            raise

    def inject(self, at, completed):
        if not completed:
            return
        bsm = [r for r in completed if r["operation"] == "BSM"]
        correction = [r for r in completed if r["operation"] == "CORRECTION"]
        self.send("INJECT {} {} {}".format(at, len(bsm), len(correction)))
        for r in bsm:
            self.send(
                "RESULT {} 1 {} {} {}".format(r["session_id"], *r["measurement_bits"], r["cause"])
            )
        for r in correction:
            self.send("CORRECT {} {}".format(r["session_id"], r["cause"]))
        row = self.read()
        if len(row) != 2 or row[0] != "INJECTED":
            raise RuntimeError("invalid INJECTED")
        self.next_time = self._time(row[1])

    def notify_ready(self, at, ready):
        if not ready:
            return
        self.send("RESOURCE_READY {} {}".format(at, len(ready)))
        for r in ready:
            self.send("RESOURCE {} {}".format(r["session_id"], r["cause"]))
        row = self.read()
        if len(row) != 2 or row[0] != "RESOURCES_NOTIFIED":
            raise RuntimeError("invalid resource-ready reply")
        self.next_time = self._time(row[1])
