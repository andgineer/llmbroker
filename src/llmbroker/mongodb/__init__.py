"""MongoDB backend: registry, store, and secrets.

Needs the ``motor`` driver (``llmbroker[mongodb]``); importing this package is
how a host declares that dependency, so a bare ``import llmbroker`` stays
driver-free. All collections are ``llmbroker_``-prefixed and owned by ``ensure_schema``.
"""

from llmbroker.mongodb.registry import Registry
from llmbroker.mongodb.secrets import Secrets
from llmbroker.mongodb.store import Store

__all__ = ["Registry", "Secrets", "Store"]
