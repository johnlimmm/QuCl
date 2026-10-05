"""Bounded line-oriented IPC and child-process lifecycle for ns-3 participants."""

import subprocess
from limits import integer, MAX_TIME_NS


class Q2nsParticipant:
    """Scenario adapters initialize the socket and perform their own handshake."""

    @staticmethod
    def _time(text):
        """Decode the no-event sentinel without accepting out-of-range timestamps."""
        value = int(text)
        if value == -1:
            return None
        return integer(value, "participant time", maximum=MAX_TIME_NS)

    def send(self, line):
        # Newlines delimit messages; sendall handles partial socket writes.
        data = (line + "\n").encode("ascii")
        self.sock.sendall(data)

    def read(self):
        # A stream read need not contain an entire message, so read through the delimiter.
        line = self.stream.readline(4098)
        if not line or not line.endswith(b"\n") or len(line) > 4097:
            raise RuntimeError("participant disconnected or sent an invalid line")
        return line.decode("ascii").split()

    def close(self):
        """Reap the child participant, including when simulation or validation fails."""
        if self.stream:
            self.stream.close()
        self.sock.close()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)

    def advance(self, at):
        self.send("ADVANCE {}".format(at))
        header = self.read()
        if len(header) != 4 or header[:2] != ["BOUNDARY", str(at)]:
            raise RuntimeError("participant boundary mismatch")
        count = int(header[3])
        if not 0 <= count <= 100000:
            raise RuntimeError("invalid participant trace count")
        self.now_ns = at
        self.next_time = self._time(header[2])
        events = []
        for _ in range(count):
            row = self.read()
            if (
                len(row) != 17
                or row[0] != "TRACE"
                or int(row[2]) != at
                or int(row[1]) != self.source_sequence
            ):
                raise RuntimeError("malformed or out-of-order participant trace")
            self.source_sequence += 1
            events.append(
                dict(
                    event_id="n:" + row[1],
                    source_sequence=int(row[1]),
                    time_ns=at,
                    event_type=row[3],
                    node_id=int(row[4]),
                    message_id=int(row[5]),
                    cause=row[6],
                    session_id=int(row[7]),
                    request_id=int(row[8]),
                    pair_a=int(row[9]),
                    pair_b=int(row[10]),
                    output_pair_id=int(row[11]),
                    m1=int(row[12]),
                    m2=int(row[13]),
                    packet_bytes=int(row[14]),
                    kind=int(row[15]),
                    queue_depth=int(row[16]),
                )
            )
        return events

    def finish(self):
        """Check quantum ownership counters, acknowledge shutdown, and reap ns-3."""
        self.send("STATUS")
        row = self.read()
        if len(row) != 5 or row[0] != "Q2NS_STATUS":
            raise RuntimeError("invalid Q2NS status")
        self.q2ns_status = dict(
            zip(
                ("native_state_count", "native_qubit_count", "sessions", "corrections_applied"),
                map(int, row[1:]),
            )
        )
        self.send("QUIT")
        if self.read() != ["BYE"]:
            raise RuntimeError("invalid shutdown acknowledgement")
        if self.process.wait(timeout=5) != 0:
            raise RuntimeError("ns-3 participant failed")
