"""Integer nanosecond and logical-identifier bounds used by both engines."""

MAX_TIME_NS = 2**53 - 1  # Exact integer range of NetSquid's double clock.
MAX_ID = 2**63 - 1


def integer(value, name, minimum=0, maximum=MAX_ID):
    """Validate integral bounds; bool is not an accepted ID or timestamp."""
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("{} must be an integer in [{}, {}]".format(name, minimum, maximum))
    return value
