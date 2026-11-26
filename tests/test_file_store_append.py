"""Rows appended at once to one FileStore journal each land whole, however the writers overlap."""

import asyncio
import json
import threading
import uuid
from datetime import UTC, datetime

from llmbroker.models import Call, CallStatus
from llmbroker.standalone.store import FileStore

THREADS = 8
CALLS_EACH = 60


def _call(i: int) -> Call:
    return Call(
        id=str(uuid.uuid4()),
        llm_name=f"m{i % 5}",
        operation=None,
        trace_id=None,
        status=CallStatus.OK if i % 2 else CallStatus.ERROR,
        ts=datetime.now(UTC),
        error_detail="x" * (i % 37),
    )


def _journal_rows(directory) -> list[dict]:
    lines = []
    for path in sorted((directory / "calls").glob("*.jsonl")):
        lines.extend(path.read_text(encoding="utf-8").splitlines())
    return [json.loads(line) for line in lines]


def test_concurrent_records_and_ratings_all_reach_the_journal(tmp_path):
    """Attempts settling together — a raced lane's failure beside its rival's answer —
    each leave their row, and so does each rating."""
    store = FileStore(tmp_path)
    calls = [_call(i) for i in range(200)]

    async def run():
        await asyncio.gather(*(store.record(call) for call in calls))
        await asyncio.gather(*(store.record_quality(call.id, 1.0) for call in calls))
        return await store.calls(limit=1000)

    rows = asyncio.run(run())
    assert {row.id for row in rows} == {call.id for call in calls}
    journal = _journal_rows(tmp_path)
    assert len(journal) == 2 * len(calls)
    assert {row["call_id"] for row in journal if "call_id" in row} == {call.id for call in calls}


def test_stores_sharing_a_directory_across_threads_lose_no_row(tmp_path):
    """Each thread drives its own store over one directory, each with its own event loop."""
    start = threading.Barrier(THREADS)
    written: list[list[str]] = [[] for _ in range(THREADS)]

    def writer(n: int) -> None:
        store = FileStore(tmp_path)
        calls = [_call(n * CALLS_EACH + i) for i in range(CALLS_EACH)]
        written[n] = [call.id for call in calls]
        start.wait()

        async def run():
            await asyncio.gather(*(store.record(call) for call in calls))

        asyncio.run(run())

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    journal = _journal_rows(tmp_path)
    assert len(journal) == THREADS * CALLS_EACH
    assert {row["id"] for row in journal} == {cid for ids in written for cid in ids}
