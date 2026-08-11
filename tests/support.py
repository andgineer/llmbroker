"""Shared helpers for unit tests that need one caller's keys."""

from llmbroker.broker.keyring import KeyRing
from llmbroker.standalone.secrets import DictSecrets


def make_ring(mapping: dict[str, str] | None = None, *, scope: str | None = None) -> KeyRing:
    """A caller's keys for a unit test: the pool's usual ref resolves to a dummy value
    unless the test states a mapping of its own."""
    return KeyRing(DictSecrets(mapping if mapping is not None else {"K": "secret"}), scope=scope)
