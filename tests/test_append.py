"""A shared append file keeps every writer's line whole, however the writers overlap."""

import asyncio
import threading
import uuid
from datetime import UTC, datetime

from llmbroker.models import Call, CallStatus
from llmbroker.standalone.store import FileStore
from llmbroker.util.append import append_bytes

THREADS = 8
LINES_EACH = 250


def test_append_creates_the_file_and_keeps_what_was_there(tmp_path):
    path = tmp_path / "journal.jsonl"
    append_bytes(path, b"first\n")
    append_bytes(path, b"second\n")
    assert path.read_bytes() == b"first\nsecond\n"


def test_append_writes_the_bytes_untranslated(tmp_path):
    """No newline translation: a journal line is the same bytes on every platform."""
    path = tmp_path / "journal.jsonl"
    append_bytes(path, "Привет\n".encode())
    assert path.read_bytes() == "Привет\n".encode()


def test_overlapping_writers_lose_no_line(tmp_path):
    """The Windows C runtime's append seeks and then writes, so two writers released
    together land on one offset and one line overwrites the other."""
    path = tmp_path / "journal.jsonl"
    start = threading.Barrier(THREADS)

    def writer(n: int) -> None:
        start.wait()
        for i in range(LINES_EACH):
            append_bytes(path, f"{n}-{i}-{'x' * (n * 7 + i % 13)}\n".encode())

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == THREADS * LINES_EACH
    assert {line.rsplit("-", 1)[0] for line in lines} == {
        f"{n}-{i}" for n in range(THREADS) for i in range(LINES_EACH)
    }


def test_concurrent_records_all_reach_the_file_journal(tmp_path):
    """Attempts settling together — a raced lane's failure beside its rival's answer —
    each leave their row."""
    store = FileStore(tmp_path)
    calls = [
        Call(
            id=str(uuid.uuid4()),
            llm_name=f"m{i % 5}",
            operation=None,
            trace_id=None,
            status=CallStatus.OK if i % 2 else CallStatus.ERROR,
            ts=datetime.now(UTC),
        )
        for i in range(200)
    ]

    async def run():
        await asyncio.gather(*(store.record(call) for call in calls))
        return await store.calls(limit=1000)

    rows = asyncio.run(run())
    assert {row.id for row in rows} == {call.id for call in calls}
