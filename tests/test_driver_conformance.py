"""One conformance suite for every ``Driver`` implementation: sqlite (file +
``:memory:``), postgres, mongodb, and the in-memory reference driver.

Behavior is tested once here — keyed-op round-trips, fetch ordering, and
journal ops (append/recent/purge) — rather than duplicated per backend.
"""

from datetime import UTC, datetime, timedelta

import pytest

from llmbroker.backends.inmemory import InMemoryDriver
from llmbroker.mongodb.driver import MongoDriver
from llmbroker.postgres.driver import PostgresDriver
from llmbroker.sqlite.driver import SqliteDriver


def _call_row(
    id_: str,
    *,
    llm_name: str,
    called_at: datetime,
    scope: str | None = None,
) -> dict[str, object]:
    return {
        "id": id_,
        "llm_name": llm_name,
        "operation": None,
        "trace_id": None,
        "status": None,
        "kind": "call",
        "http_status": None,
        "latency_ms": None,
        "error_detail": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "usage_extra": None,
        "quality_score": None,
        "call_id": None,
        "called_at": called_at,
        "scope": scope,
        "cooldown_until": None,
        "key_hash": None,
    }


_PG_TABLES = ("llmbroker_registry", "llmbroker_secrets", "llmbroker_disabled", "llmbroker_calls")


@pytest.fixture(
    params=["sqlite_file", "sqlite_memory", "postgres", "mongodb", "inmemory"],
    ids=["sqlite_file", "sqlite_memory", "postgres", "mongodb", "inmemory"],
)
async def driver(request, tmp_path_factory, pg_pool, mongo_db):
    param = request.param
    if param == "sqlite_file":
        db_path = str(tmp_path_factory.mktemp("conformance_sqlite") / "d.db")
        yield SqliteDriver(db_path)
    elif param == "sqlite_memory":
        yield SqliteDriver(":memory:")
    elif param == "postgres":
        try:
            yield PostgresDriver(pg_pool)
        finally:
            async with pg_pool.acquire() as conn:
                for table in _PG_TABLES:
                    await conn.execute(f"DELETE FROM {table}")  # noqa: S608
    elif param == "mongodb":
        try:
            yield MongoDriver(mongo_db)
        finally:
            for table in _PG_TABLES:
                await mongo_db[table].delete_many({})
    elif param == "inmemory":
        yield InMemoryDriver()


# ── Keyed-record round-trips (registry/disabled/secrets) ────────────────────


async def test_get_missing_returns_none(driver):
    assert await driver.get("secrets", ("missing-ref",)) is None


async def test_upsert_then_get_round_trips(driver):
    await driver.upsert("secrets", ("K",), {"ref": "K", "value": "v1"})
    row = await driver.get("secrets", ("K",))
    assert row["ref"] == "K"
    assert row["value"] == "v1"


async def test_upsert_overwrites_existing(driver):
    await driver.upsert("secrets", ("K",), {"ref": "K", "value": "v1"})
    await driver.upsert("secrets", ("K",), {"ref": "K", "value": "v2"})
    row = await driver.get("secrets", ("K",))
    assert row["value"] == "v2"


async def test_delete_returns_true_when_present_false_when_absent(driver):
    await driver.upsert("secrets", ("K",), {"ref": "K", "value": "v"})
    assert await driver.delete("secrets", ("K",)) is True
    assert await driver.delete("secrets", ("K",)) is False
    assert await driver.get("secrets", ("K",)) is None


async def test_slash_containing_key_round_trips(driver):
    """Secret refs like the broker's own-scope prefix (``user/42/GROQ_API_KEY``)
    must round-trip as a plain opaque key — no path/segment handling anywhere."""
    ref = "user/42/GROQ_API_KEY"
    await driver.upsert("secrets", (ref,), {"ref": ref, "value": "sk-x"})
    row = await driver.get("secrets", (ref,))
    assert row["value"] == "sk-x"
    assert await driver.delete("secrets", (ref,)) is True


async def test_fetch_orders_by_key(driver):
    for name in ("charlie", "alice", "bob"):
        await driver.upsert(
            "registry",
            (name,),
            {
                "name": name,
                "base_url": "https://x/v1",
                "model": "m",
                "api_key_ref": "K",
                "metadata": {},
            },
        )
    rows = await driver.fetch("registry")
    assert [r["name"] for r in rows] == ["alice", "bob", "charlie"]


async def test_fetch_empty_table_returns_empty_list(driver):
    assert await driver.fetch("disabled") == []


async def test_json_column_round_trips_nested_metadata(driver):
    await driver.upsert(
        "registry",
        ("p1",),
        {
            "name": "p1",
            "base_url": "https://x/v1",
            "model": "m",
            "api_key_ref": "K",
            "metadata": {"parallel": 3},
        },
    )
    row = await driver.get("registry", ("p1",))
    assert row["metadata"] == {"parallel": 3}


# ── Journal ops (append/recent/purge) ────────────────────────────────────────


async def test_journal_append_and_recent_newest_first(driver):
    base = datetime(2030, 1, 1, tzinfo=UTC)
    for i, name in enumerate(("c1", "c2", "c3")):
        await driver.append(
            "calls", _call_row(name, llm_name="x", called_at=base + timedelta(seconds=i))
        )
    rows = await driver.recent("calls", 10)
    assert [r["id"] for r in rows] == ["c3", "c2", "c1"]


async def test_journal_recent_respects_limit(driver):
    base = datetime(2030, 1, 1, tzinfo=UTC)
    for i in range(5):
        await driver.append(
            "calls", _call_row(f"c{i}", llm_name="x", called_at=base + timedelta(seconds=i))
        )
    rows = await driver.recent("calls", 2)
    assert [r["id"] for r in rows] == ["c4", "c3"]


async def test_journal_recent_with_match_filter(driver):
    base = datetime(2030, 1, 1, tzinfo=UTC)
    await driver.append("calls", _call_row("c1", llm_name="x", called_at=base, scope="alice"))
    await driver.append(
        "calls",
        _call_row("c2", llm_name="x", called_at=base + timedelta(seconds=1), scope="bob"),
    )
    rows = await driver.recent("calls", 10, match={"scope": "alice"})
    assert [r["id"] for r in rows] == ["c1"]


async def test_journal_recent_with_none_match_value_matches_null(driver):
    base = datetime(2030, 1, 1, tzinfo=UTC)
    await driver.append("calls", _call_row("c1", llm_name="x", called_at=base, scope=None))
    await driver.append(
        "calls",
        _call_row("c2", llm_name="x", called_at=base + timedelta(seconds=1), scope="bob"),
    )
    rows = await driver.recent("calls", 10, match={"scope": None})
    assert [r["id"] for r in rows] == ["c1"]


async def test_journal_purge_removes_rows_before_cutoff(driver):
    old = datetime(2020, 1, 1, tzinfo=UTC)
    new = datetime(2030, 1, 1, tzinfo=UTC)
    await driver.append("calls", _call_row("old", llm_name="x", called_at=old))
    await driver.append("calls", _call_row("new", llm_name="x", called_at=new))
    removed = await driver.purge("calls", datetime(2025, 1, 1, tzinfo=UTC))
    assert removed == 1
    rows = await driver.recent("calls", 10)
    assert [r["id"] for r in rows] == ["new"]


# ── MongoDB: naive (pre-tz_aware) datetime handling ──────────────────────────


async def test_mongodb_naive_datetime_reattaches_utc(mongo_db):
    """The client is never opened with ``tz_aware=True``, so pymongo returns naive
    datetimes for BSON dates written by any external/legacy writer; reads must
    still reattach UTC rather than misreading them as local time."""
    driver_ = MongoDriver(mongo_db)
    await driver_.ensure_schema()
    naive = datetime(2030, 1, 1, 12, 0, 0)  # no tzinfo, as a raw BSON write would read back
    await mongo_db["llmbroker_calls"].insert_one(
        {"id": "legacy", "llm_name": "x", "kind": "call", "called_at": naive},
    )
    try:
        rows = await driver_.recent("calls", 10)
        row = next(r for r in rows if r["id"] == "legacy")
        assert row["called_at"].tzinfo is not None
    finally:
        await mongo_db["llmbroker_calls"].delete_many({"id": "legacy"})
