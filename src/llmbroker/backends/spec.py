"""One declarative description of the stores, consumed by every driver's
``ensure_schema``. Column types are portable strings — each driver maps them to
its own native DDL/wire representation.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TableSpec:
    """One store's shape: name, identity columns, and portable column types. Every
    key column is a single non-null text column, and the user scope rides inside the
    ref string rather than in a column of its own."""

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
            "budget_ms": "int",
        },
        indexes=(("llm_name",), ("called_at",), ("trace_id",), ("call_id",)),
    ),
}

# Gates the current TABLES shape; ensure_schema creates it fresh or raises on mismatch.
SCHEMA_VERSION = 7

# The columns every driver's journal fold names directly in its query.
JOURNAL_FOLD_COLUMNS = ("id", "called_at", "kind", "call_id", "quality_score")


def _check_fold_columns(columns: dict[str, str]) -> None:
    """Refuse to load when a fold column is gone from the journal: each driver spells
    these out, so a rename reaching only this file diverges per backend — SQL fails,
    Mongo keeps answering off the old name."""
    missing = [col for col in JOURNAL_FOLD_COLUMNS if col not in columns]
    if missing:
        raise RuntimeError(
            f"journal fold column(s) {missing} are not in the calls table — every"
            " driver's journal_view names them directly, so rename them there too",
        )


_check_fold_columns(TABLES["calls"].columns)
