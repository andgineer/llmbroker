"""Redis backend — StateStore over Redis hashes.

Needs the ``redis`` driver (``llmbroker[redis]``). All keys are
``llmbroker_``-prefixed.
"""

from llmbroker.redis.state_store import StateStore

__all__ = ["StateStore"]
