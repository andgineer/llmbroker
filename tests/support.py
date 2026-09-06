"""Shared helpers for unit tests that need one caller's keys."""

import time

from llmbroker.broker.keyring import KeyRing
from llmbroker.standalone.secrets import DictSecrets

# An asyncio timer may fire up to one clock resolution early, and the same coarse clock
# quantizes the measurement of it: Windows' monotonic ticks every 15.6 ms, Linux's every
# nanosecond. Any assertion that a wall-clock span reached its budget needs this slack.
CLOCK_SLACK = 2 * time.get_clock_info("monotonic").resolution


def make_ring(mapping: dict[str, str] | None = None, *, scope: str | None = None) -> KeyRing:
    """A caller's keys for a unit test: the pool's usual ref resolves to a dummy value
    unless the test states a mapping of its own."""
    return KeyRing(DictSecrets(mapping if mapping is not None else {"K": "secret"}), scope=scope)
