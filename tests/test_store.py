"""Tests for the standalone stores: InMemoryStore, FileStore."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from llmbroker.models import Call, CallStatus
from llmbroker.standalone.store import FileStore, InMemoryStore

_TODAY = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
_YESTERDAY = _TODAY - timedelta(days=1)


def _call(call_id="c1", llm_name="p1", ts=None):
    return Call(
        id=call_id,
        llm_name=llm_name,
        operation="test",
        trace_id=None,
        status=CallStatus.OK,
        ts=ts if ts is not None else _TODAY,
        http_status=200,
        latency_ms=100,
        error_detail=None,
        usage=None,
    )


def test_in_memory_store_record_does_not_raise():
    asyncio.run(InMemoryStore().record(_call()))


def test_in_memory_store_record_quality_does_not_raise():
    asyncio.run(InMemoryStore().record_quality("p1", "summarize", 1.0, call_id="c1"))


def test_in_memory_store_disabled_map_is_in_memory_only():
    async def run():
        k = InMemoryStore()
        await k.seed_disabled(["p1", "p2"])
        assert await k.disabled_map() == {"p1": False, "p2": False}
        await k.set_disabled("p1", True)
        assert await k.get_disabled("p1") is True
        assert await k.get_disabled("p2") is False

    asyncio.run(run())


def test_file_store_since_skips_whole_expired_day_files(tmp_path, monkeypatch):
    """The day-file layout makes the bound cheap: a file whose whole date precedes
    ``since`` is never opened, rather than read and discarded row by row."""

    async def run():
        store = FileStore(tmp_path)
        await store.record(_call("old", ts=_YESTERDAY))
        await store.record(_call("new", ts=_TODAY))

        opened: list[str] = []
        real_read_text = Path.read_text

        def spy(self, *args, **kwargs):
            opened.append(self.name)
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", spy)
        rows = await store.calls(limit=10, since=_TODAY.replace(hour=0))
        assert [c.id for c in rows] == ["new"]
        assert f"{_YESTERDAY.date().isoformat()}.jsonl" not in opened

    asyncio.run(run())


def test_file_store_day_file_is_named_by_utc_date_not_the_records_offset(tmp_path):
    """The ``since`` bound skips whole files by their name, so a record whose own
    offset puts it on a different calendar day must still land in its UTC day file —
    otherwise the skip drops a row that is inside the window, with no second chance."""

    async def run():
        store = FileStore(tmp_path)
        west = timezone(timedelta(hours=-5))
        # 2029-12-31T23:00-05:00 is 2030-01-01T04:00Z — a UTC-day-1 record.
        ts = datetime(2029, 12, 31, 23, 0, tzinfo=west)
        await store.record(_call("crosses-midnight", ts=ts))

        assert (tmp_path / "calls" / "2030-01-01.jsonl").exists()
        rows = await store.calls(limit=10, since=datetime(2030, 1, 1, tzinfo=UTC))
        assert [c.id for c in rows] == ["crosses-midnight"]

    asyncio.run(run())


def test_file_store_since_whose_local_date_leads_its_utc_date(tmp_path):
    """The skip compares ``since.date()``, so the bound must already be UTC: an
    eastern bound whose calendar day has rolled over would otherwise skip the day
    file holding rows that are still inside the window."""

    async def run():
        store = FileStore(tmp_path)
        await store.record(_call("late-jan1", ts=datetime(2030, 1, 1, 22, tzinfo=UTC)))
        # 2030-01-02T02:00+05:00 is 2030-01-01T21:00Z — an hour before the row.
        bound = datetime(2030, 1, 2, 2, tzinfo=timezone(timedelta(hours=5)))
        assert bound.date() > bound.astimezone(UTC).date()  # guards the fixture
        rows = await store.calls(limit=10, since=bound)
        assert [c.id for c in rows] == ["late-jan1"]

    asyncio.run(run())


def test_file_store_rejects_naive_ts_on_record(tmp_path):
    """Refused at the write boundary, not silently stored: a naive ts would make the
    day file machine-timezone-dependent and crash every later windowed read."""

    async def run():
        with pytest.raises(ValueError, match="timezone-aware"):
            await FileStore(tmp_path).record(_call("naive", ts=datetime(2030, 1, 1, 2)))  # noqa: DTZ001

    asyncio.run(run())


def test_file_store_record_normalizes_offset_to_utc(tmp_path):
    """A record written in another offset reads back as the same instant in UTC."""

    async def run():
        store = FileStore(tmp_path)
        eastern = datetime(2030, 1, 1, 13, tzinfo=timezone(timedelta(hours=5)))
        await store.record(_call("eastern", ts=eastern))
        (row,) = await store.calls(limit=10)
        assert row.ts == eastern
        assert row.ts.utcoffset() == timedelta(0)

    asyncio.run(run())


def test_file_store_rejects_naive_since(tmp_path):
    async def run():
        with pytest.raises(ValueError, match="timezone-aware"):
            await FileStore(tmp_path).calls(limit=10, since=datetime(2030, 1, 1))  # noqa: DTZ001

    asyncio.run(run())


def test_file_store_since_is_inclusive_at_the_bound(tmp_path):
    async def run():
        store = FileStore(tmp_path)
        await store.record(_call("exactly-at", ts=_TODAY))
        rows = await store.calls(limit=10, since=_TODAY)
        assert [c.id for c in rows] == ["exactly-at"]

    asyncio.run(run())


def test_file_store_since_drops_older_rows_inside_a_kept_day_file(tmp_path):
    """A day file that straddles the bound is read, but its pre-bound rows are dropped."""

    async def run():
        store = FileStore(tmp_path)
        await store.record(_call("early", ts=_TODAY.replace(hour=1)))
        await store.record(_call("late", ts=_TODAY.replace(hour=23)))
        rows = await store.calls(limit=10, since=_TODAY.replace(hour=12))
        assert [c.id for c in rows] == ["late"]

    asyncio.run(run())


def test_file_store_record_writes_day_file(tmp_path):
    asyncio.run(FileStore(tmp_path).record(_call(ts=_TODAY)))
    path = tmp_path / "calls" / f"{_TODAY.date().isoformat()}.jsonl"
    line = json.loads(path.read_text())
    assert line["kind"] == "call"
    assert line["id"] == "c1"
    assert line["llm_name"] == "p1"
    assert line["status"] == "ok"
    assert line["http_status"] == 200


def test_file_store_record_quality_writes_line(tmp_path):
    asyncio.run(FileStore(tmp_path).record_quality("p1", "summarize", 0.8, call_id="c1"))
    day_files = list((tmp_path / "calls").glob("*.jsonl"))
    assert len(day_files) == 1
    line = json.loads(day_files[0].read_text())
    assert line["kind"] == "quality"
    assert line["llm_name"] == "p1"
    assert line["operation"] == "summarize"
    assert line["call_id"] == "c1"
    assert line["quality_score"] == 0.8
    assert "status" not in line  # None fields are dropped at serialization


def test_file_store_record_appends_multiple_same_day(tmp_path):
    k = FileStore(tmp_path)
    asyncio.run(k.record(_call("c1", ts=_TODAY)))
    asyncio.run(k.record(_call("c2", ts=_TODAY)))
    path = tmp_path / "calls" / f"{_TODAY.date().isoformat()}.jsonl"
    lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert len(lines) == 2
    assert lines[0]["id"] == "c1"
    assert lines[1]["id"] == "c2"


def test_file_store_splits_by_day(tmp_path):
    k = FileStore(tmp_path)
    asyncio.run(k.record(_call("c1", ts=_YESTERDAY)))
    asyncio.run(k.record(_call("c2", ts=_TODAY)))
    assert (tmp_path / "calls" / f"{_YESTERDAY.date().isoformat()}.jsonl").exists()
    assert (tmp_path / "calls" / f"{_TODAY.date().isoformat()}.jsonl").exists()


def test_file_store_calls_reads_newest_first_across_days(tmp_path):
    k = FileStore(tmp_path)

    async def run():
        await k.record(_call("c1", ts=_YESTERDAY))
        await k.record(_call("c2", ts=_TODAY))
        return await k.calls(limit=10)

    calls = asyncio.run(run())
    assert [c.id for c in calls] == ["c2", "c1"]


def test_file_store_calls_respects_limit(tmp_path):
    k = FileStore(tmp_path)

    async def run():
        for i in range(5):
            await k.record(_call(f"c{i}", ts=_TODAY))
        return await k.calls(limit=2)

    calls = asyncio.run(run())
    assert len(calls) == 2
    assert [c.id for c in calls] == ["c4", "c3"]


def test_file_store_calls_scope_filter(tmp_path):
    k = FileStore(tmp_path)

    async def run():
        await k.record(replace(_call("ca"), scope="alice"))
        await k.record(replace(_call("cb"), scope="bob"))
        return await k.calls(limit=10, scope="alice")

    calls = asyncio.run(run())
    assert [c.id for c in calls] == ["ca"]


def test_file_store_disabled_map_persists_to_yaml_file(tmp_path):
    async def run():
        k = FileStore(tmp_path)
        await k.seed_disabled(["p1"])
        await k.set_disabled("p1", True)
        # A fresh instance reading the same directory sees the persisted verdict.
        k2 = FileStore(tmp_path)
        return await k2.get_disabled("p1")

    assert asyncio.run(run()) is True
    assert (tmp_path / "disabled.yml").exists()


def test_file_store_seed_disabled_never_overwrites_existing_value(tmp_path):
    k = FileStore(tmp_path)

    async def run():
        await k.set_disabled("p1", True)
        await k.seed_disabled(["p1", "p2"])
        return await k.disabled_map()

    result = asyncio.run(run())
    assert result == {"p1": True, "p2": False}


def test_file_store_purges_day_files_older_than_retention(tmp_path):
    old_ts = datetime.now(UTC) - timedelta(days=100)
    recent_ts = datetime.now(UTC) - timedelta(days=1)
    k = FileStore(tmp_path, retention=timedelta(days=90))

    async def run():
        await k.record(_call("old", ts=old_ts))
        await k.record(_call("recent", ts=recent_ts))

    asyncio.run(run())
    remaining = {c.id for c in asyncio.run(k.calls(limit=10))}
    assert remaining == {"recent"}


def test_file_store_retention_purge_is_debounced(tmp_path):
    """A second write within the debounce window does not re-run the purge scan —
    verified indirectly: an old file written after the first purge survives until
    the debounce interval elapses again."""
    old_ts = datetime.now(UTC) - timedelta(days=100)
    k = FileStore(tmp_path, retention=timedelta(days=90))

    async def run():
        await k.record(_call("triggers-purge", ts=datetime.now(UTC)))  # closes the debounce
        await k.record(_call("old", ts=old_ts))  # written after purge already ran once

    asyncio.run(run())
    remaining = {c.id for c in asyncio.run(k.calls(limit=10))}
    assert "old" in remaining  # not purged — debounce window hasn't elapsed
