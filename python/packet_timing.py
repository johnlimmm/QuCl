"""Serialization time for IPv4/UDP over a point-to-point link."""


def tx_duration_ns(payload_bytes, rate_bps):
    # No IP options or fragmentation: PPP 2 + IPv4 20 + UDP 8 bytes.
    # Round positive durations to integer ns; configuration rejects exact half-ns ties.
    numerator = (payload_bytes + 30) * 8 * 1000000000
    return (2 * numerator + rate_bps) // (2 * rate_bps)
