"""Postgres backend: registry, store, and secrets.

Needs the ``asyncpg`` driver (``llmbroker[postgres]``); importing this package is
how a host declares that dependency, so a bare ``import llmbroker`` stays
driver-free. All tables are ``llmbroker_``-prefixed and owned by ``ensure_schema``.
"""

from llmbroker.postgres.registry import Registry
from llmbroker.postgres.secrets import Secrets
from llmbroker.postgres.store import Store

__all__ = ["Registry", "Secrets", "Store"]
