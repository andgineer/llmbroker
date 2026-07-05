"""One declarative description of the stores, consumed by every driver's
``ensure_schema``. Column types are portable strings — each driver maps them to
its own native DDL/wire representation.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TableSpec:
    """One store's shape: name, identity columns, and portable column types.

    Every key column is a single non-null text column: registry (``name``,),
    disabled (``name``,), secrets (``ref``,) — the user scope rides inside the
    ref string, so no scope columns exist anywhere. The calls journal has no
    keyed access (append-only); its ``id`` column is listed for completeness
    and uniqueness only.
    """

    name: str
    key: tuple[str, ...]
    columns: dict[str, str] = field(default_factory=dict)
    indexes: tuple[tuple[str, ...], ...] = ()


TABLES: dict[str, TableSpec] = {
    "registry": TableSpec(
        name="llmbroker_registry",
        key=("name",),
        columns={
            "name": "text",
            "base_url": "text",
            "model": "text",
            "api_key_ref": "text",
            "metadata": "json",
        },
    ),
    "disabled": TableSpec(
        name="llmbroker_disabled",
        key=("name",),
        columns={
            "name": "text",
            "disabled": "int",
        },
    ),
    "secrets": TableSpec(
        name="llmbroker_secrets",
        key=("ref",),
        columns={
            "ref": "text",
            "value": "text",
        },
    ),
    "calls": TableSpec(
        name="llmbroker_calls",
        key=("id",),
        columns={
            "id": "text",
            "llm_name": "text",
            "operation": "text",
            "trace_id": "text",
            "status": "text",
            "kind": "text",
            "http_status": "int",
            "latency_ms": "int",
            "error_detail": "text",
            "prompt_tokens": "int",
            "completion_tokens": "int",
            "total_tokens": "int",
            "usage_extra": "json",
            "quality_score": "real",
            "call_id": "text",
            "called_at": "timestamp",
            "scope": "text",
            "cooldown_until": "timestamp",
            "key_hash": "text",
        },
        indexes=(("llm_name",), ("called_at",)),
    ),
}

# Bump rationale: summaries and state tables are gone (Plan 2 — shared cooldowns
# derive from the journal); registry becomes a pure preset mirror (user_id, origin,
# profile columns gone); calls gains kind, cooldown_until, key_hash; its attribution
# column is scope (text); secrets keyed by ref alone (scope is a ref prefix); new
# tiny llmbroker_disabled holds admin verdicts.
SCHEMA_VERSION = 5
