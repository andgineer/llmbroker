"""Postgres backend: registry, store, and secrets.

Needs the ``asyncpg`` driver (``llmbroker[postgres]``). All tables are
``llmbroker_``-prefixed and owned by ``ensure_schema``.
"""
