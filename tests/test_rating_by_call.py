"""The delayed rating entry point: a rating names the call it rates, by the call's
own id or by the trace the request was made under."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from llmbroker.broker import llms as llms_module
from llmbroker.broker.broker import AsyncBroker
from llmbroker.exceptions import UnknownCallError
from llmbroker.models import Call, CallStatus
from llmbroker.optimizer import Optimizer
from llmbroker.sqlite import Store as SqliteStore
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import InMemoryStore

_PATCH = "llmbroker.broker.router.call_provider"


def _registry(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n'
        '[[llms]]\nname="p2"\nbase_url="https://y/v1"\nmodel="m"\napi_key_ref="K"\n',
    )
    return Registry(f)


def _broker(tmp_path, store=None, optimize=True) -> AsyncBroker:
    return AsyncBroker(
        registry=_registry(tmp_path),
        secrets=DictSecrets({"K": "test"}),
        store=store if store is not None else SqliteStore(str(tmp_path / "b.db")),
        optimize=optimize,
        sync=None,
    )


async def _journal(broker, id_, **kw) -> str:
    """Put one call row in the journal for a rating to name."""
    await broker._store.record(
        Call(
            id=id_,
            llm_name=kw.pop("llm_name", "p1"),
            operation=kw.pop("operation", "summarize"),
            trace_id=kw.pop("trace_id", None),
            status=kw.pop("status", CallStatus.OK),
            ts=datetime.now(UTC),
            **kw,
        ),
    )
    return id_


async def _stored_ratings(db_path) -> list[tuple]:
    """``calls()`` folds a rating onto the call it names, so the rating row's own
    columns are visible nowhere but the raw journal."""
    async with aiosqlite.connect(db_path) as db:
        rows = await (
            await db.execute(
                "SELECT call_id, quality_score, scope FROM llmbroker_calls"
                " WHERE kind = 'quality' ORDER BY called_at",
            )
        ).fetchall()
    return [tuple(row) for row in rows]


class _CountingStore:
    """Queryable store that counts the journal reads its owner performs."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.reads = 0

    async def record(self, call) -> None:
        await self._inner.record(call)

    async def record_quality(self, call_id, score, *, scope=None) -> None:
        await self._inner.record_quality(call_id, score, scope=scope)

    async def calls(self, **kw):
        self.reads += 1
        return await self._inner.calls(**kw)


async def test_record_quality_by_call_id_rates_that_model(tmp_path):
    """The model and the operation are read off the resolved call, never supplied."""
    async with _broker(tmp_path) as broker:
        await _journal(broker, "c1")
        await broker.record_quality(0.25, call_id="c1")

        (row,) = await broker.calls(limit=10)
        assert (row.id, row.score) == ("c1", pytest.approx(0.25))
        assert list(broker._optimizer._scores[("p1", "summarize")]) == [("c1", 0.25)]


async def test_record_quality_by_trace_id_rates_every_answering_attempt(tmp_path):
    """One verdict about one request lands on every model that answered under it."""
    async with _broker(tmp_path) as broker:
        await _journal(broker, "a1", llm_name="p1", trace_id="req-1")
        await _journal(broker, "a2", llm_name="p2", trace_id="req-1")
        await _journal(broker, "other", llm_name="p1", trace_id="req-2")
        await broker.record_quality(1.0, trace_id="req-1")

        scored = {r.id: r.score for r in await broker.calls(limit=10)}
        assert scored == {"a1": 1.0, "a2": 1.0, "other": None}
        assert list(broker._optimizer._scores[("p1", "summarize")]) == [("a1", 1.0)]
        assert list(broker._optimizer._scores[("p2", "summarize")]) == [("a2", 1.0)]


async def test_record_quality_by_trace_id_ignores_the_attempts_that_failed(tmp_path):
    """A rate-limited attempt produced nothing to judge, so it is not rated and does
    not enter any quality window."""
    async with _broker(tmp_path) as broker:
        await _journal(
            broker, "failed", llm_name="p2", trace_id="req-1", status=CallStatus.RATE_LIMITED
        )
        await _journal(broker, "answered", llm_name="p1", trace_id="req-1")
        await broker.record_quality(0.0, trace_id="req-1")

        scored = {r.id: r.score for r in await broker.calls(limit=10)}
        assert scored == {"failed": None, "answered": 0.0}
        assert ("p2", "summarize") not in broker._optimizer._scores


async def test_record_quality_raises_when_nothing_resolves(tmp_path):
    async with _broker(tmp_path) as broker:
        with pytest.raises(UnknownCallError, match="no answered call"):
            await broker.record_quality(1.0, call_id="never-happened")


async def test_record_quality_raises_when_every_attempt_failed(tmp_path):
    """A trace whose every attempt failed names nothing rateable — loud, not silent."""
    async with _broker(tmp_path) as broker:
        await _journal(broker, "failed", trace_id="req-1", status=CallStatus.ERROR)
        with pytest.raises(UnknownCallError, match="req-1"):
            await broker.record_quality(0.0, trace_id="req-1")


@pytest.mark.parametrize(
    "keys",
    [{}, {"call_id": "c1", "trace_id": "req-1"}],
    ids=["neither", "both"],
)
async def test_record_quality_requires_exactly_one_key(tmp_path, keys):
    async with _broker(tmp_path) as broker:
        with pytest.raises(ValueError, match="exactly one of call_id= or trace_id="):
            await broker.record_quality(1.0, **keys)


async def test_record_quality_warns_when_the_resolution_read_comes_back_full(
    tmp_path, monkeypatch, caplog
):
    """A page that comes back full may have left attempts unrated, so the host hears
    about it rather than getting a silently partial rating."""
    monkeypatch.setattr(llms_module, "_RATING_PAGE", 2)
    async with _broker(tmp_path) as broker:
        for i in range(3):
            await _journal(broker, f"a{i}", trace_id="req-1")
        with caplog.at_level("WARNING", logger="llmbroker.broker"):
            await broker.record_quality(1.0, trace_id="req-1")
        assert "req-1" in caplog.text
        assert len([r for r in await broker.calls(limit=10) if r.score is not None]) == 2


async def test_result_record_quality_needs_no_read(tmp_path):
    """The live handle already holds the call id, the model and the operation."""
    store = _CountingStore(SqliteStore(str(tmp_path / "b.db")))
    async with _broker(tmp_path, store) as broker:
        with patch(_PATCH, new=AsyncMock(return_value=("ok", None, None))):
            result = await broker.ask("x", operation="summarize")
        store.reads = 0  # the pool build's own tail read is not this test's subject

        await result.record_quality(1.0)
        assert store.reads == 0

        await broker.record_quality(1.0, call_id=result.call_id)
        assert store.reads == 1


async def test_scoped_caller_resolves_and_writes_its_own_scope(tmp_path):
    """The scope comes from the caller object, not from the key: two callers sharing
    one trace each rate only their own attempt."""
    async with _broker(tmp_path) as broker:
        await _journal(broker, "alice-call", llm_name="p1", trace_id="req-1", scope="alice")
        await _journal(broker, "bob-call", llm_name="p2", trace_id="req-1", scope="bob")

        await broker.for_scope("alice").record_quality(1.0, trace_id="req-1")

        scored = {r.id: r.score for r in await broker.calls(limit=10)}
        assert scored == {"alice-call": 1.0, "bob-call": None}
        assert list(broker._optimizer._scores[("p1", "summarize")]) == [("alice-call", 1.0)]
        assert ("p2", "summarize") not in broker._optimizer._scores


async def test_a_caller_stamps_its_scope_on_the_rating_row(tmp_path):
    """The scope reaches the journal as attribution on the rating row itself. The fold
    never reads it back, so the raw journal is the only place it can be seen at all."""
    db = str(tmp_path / "b.db")
    async with _broker(tmp_path, SqliteStore(db)) as broker:
        await _journal(broker, "c1", scope="alice")
        await broker.for_scope("alice").record_quality(0.25, call_id="c1")

        assert await _stored_ratings(db) == [("c1", pytest.approx(0.25), "alice")]


async def test_record_quality_rates_a_model_that_left_the_pool(tmp_path):
    """The model is read off the journal row, not looked up in the pool: an entry the
    registry no longer carries is still rateable, and its verdict still counts."""
    async with _broker(tmp_path) as broker:
        await _journal(broker, "orphan", llm_name="ghost")
        await broker.record_quality(0.0, call_id="orphan")

        assert list(broker._optimizer._scores[("ghost", "summarize")]) == [("orphan", 0.0)]


async def test_re_rating_one_call_cannot_demote_a_model(tmp_path):
    """Ten verdicts on one answer stay one observation, so the minimum-count guard is
    not something a host clears by rating the same call over and over."""
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    async with _broker(tmp_path, optimize=opt) as broker:
        await _journal(broker, "c1")
        for _ in range(10):
            await broker.record_quality(0.0, call_id="c1")

        assert list(opt._scores[("p1", "summarize")]) == [("c1", 0.0)]
        assert opt.is_demoted("p1", "summarize") is False


async def test_rating_one_result_twice_is_one_vote(tmp_path):
    """The live handle holds its call id, so keying the window by the call makes the
    read-free path safe against a repeat too — the newer verdict simply replaces."""
    store = _CountingStore(SqliteStore(str(tmp_path / "b.db")))
    async with _broker(tmp_path, store) as broker:
        with patch(_PATCH, new=AsyncMock(return_value=("ok", None, None))):
            result = await broker.ask("x", operation="summarize")
        store.reads = 0

        await result.record_quality(0.0)
        await result.record_quality(1.0)

        assert store.reads == 0
        assert list(broker._optimizer._scores[("p1", "summarize")]) == [(result.call_id, 1.0)]


async def test_record_quality_on_a_non_queryable_store_raises_typeerror(tmp_path):
    """The key must be resolved against the journal, so a store with no read path
    surfaces the same refusal ``calls()`` already raises."""
    async with _broker(tmp_path, InMemoryStore()) as broker:
        with pytest.raises(TypeError, match="not queryable"):
            await broker.record_quality(1.0, call_id="c1")
