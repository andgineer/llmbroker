"""SQLite backend: registry, store, and secrets over one DB file.

Needs the ``aiosqlite`` driver (``llmbroker[sqlite]``); importing a submodule is
how a host declares that dependency, so a bare ``import llmbroker`` stays
driver-free. All tables are ``llmbroker_``-prefixed and owned by ``ensure_schema``.
"""
