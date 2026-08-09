"""Tests for AsyncBroker core routing, sync(), error escalation."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from llmbroker.backends.ports import DriverStore
from llmbroker.broker import presets
from llmbroker.broker.broker import AsyncBroker
from llmbroker.exceptions import EmptyRegistryError, NoLLMAvailableError
from llmbroker.models import CallStatus, LifecyclePhase, LLMConfig
from llmbroker.optimizer import Optimizer
from llmbroker.protocols.store import QueryableStoreProtocol
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.sqlite import Secrets as SqliteSecrets
from llmbroker.sqlite import Store as SqliteStore
from llmbroker.standalone.registry import Registry as FileRegistry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import FileStore, InMemoryStore


def _registry(tmp_path, entries=None, filename="llms.toml"):
    if entries is None:
        entries = [("p1", "https://x/v1", "m", "K")]
    lines = []
    for name, base_url, model, ref in entries:
        lines += [
            "[[llms]]",
            f'name="{name}"',
            f'base_url="{base_url}"',
            f'model="{model}"',
            f'api_key_ref="{ref}"',
        ]
    f = tmp_path / filename
    f.write_text("\n".join(lines) + "\n")
    return FileRegistry(f)


def _http_ok(content="hello"):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=False)
    cm.post = AsyncMock(return_value=resp)
    cm.aclose = AsyncMock()
    return cm


def _http_error(status):
    mock_request = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = status
    mock_response.headers = {}
    mock_response.text = f"HTTP {status}"
    exc = httpx.HTTPStatusError("err", request=mock_request, response=mock_response)
    resp = MagicMock()
    resp.raise_for_status.side_effect = exc
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=False)
    cm.post = AsyncMock(return_value=resp)
    cm.aclose = AsyncMock()
    return cm


def test_ensure_pool_populates_configs(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), store=InMemoryStore()) as broker:
            assert await broker.count() == 1
            assert (await broker.get("p1")).config.name == "p1"

    asyncio.run(run())


def test_ensure_pool_idempotent(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), store=InMemoryStore()) as broker:
            await broker.ensure_pool()  # second call after __aenter__ — must be no-op
            assert await broker.count() == 1

    asyncio.run(run())


def test_snapshot_names(tmp_path):
    async def run():
        entries = [("a", "https://a/v1", "m", "K"), ("b", "https://b/v1", "m", "K")]
        async with AsyncBroker(
            registry=_registry(tmp_path, entries), store=InMemoryStore()
        ) as broker:
            assert set((await broker.snapshot()).keys()) == {"a", "b"}

    asyncio.run(run())


def test_get_returns_async_llm_with_correct_config(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), store=InMemoryStore()) as broker:
            llm = await broker.get("p1")
            assert llm.config.name == "p1"
            assert llm.config.base_url == "https://x/v1"

    asyncio.run(run())


def test_get_missing_raises_key_error(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), store=InMemoryStore()) as broker:
            with pytest.raises(KeyError):
                await broker.get("nope")

    asyncio.run(run())


def test_async_llm_state_available(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), store=InMemoryStore()) as broker:
            state = await (await broker.get("p1")).state()
            assert state.phase is LifecyclePhase.AVAILABLE

    asyncio.run(run())


def test_async_llm_metrics_no_queryable_store(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), store=InMemoryStore()) as broker:
            metrics = await (await broker.get("p1")).metrics()
            assert metrics.call_count == 0

    asyncio.run(run())


def _secrets() -> DictSecrets:
    return DictSecrets({"K": "test"})


def test_chat_happy_path(tmp_path):
    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path), secrets=_secrets(), store=InMemoryStore()
        ) as broker:
            with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_ok("world")):
                result = await broker.chat([{"role": "user", "content": "hi"}])
                assert result.text == "world"

    asyncio.run(run())


def test_ask_delegates_to_chat(tmp_path):
    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path), secrets=_secrets(), store=InMemoryStore()
        ) as broker:
            with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_ok("yes")):
                result = await broker.ask("prompt")
                assert result.text == "yes"

    asyncio.run(run())


def test_chat_missing_key_raises_no_llm_available(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), store=InMemoryStore()) as broker:
            with pytest.raises(NoLLMAvailableError, match="api_key_ref") as exc_info:
                await broker.chat([{"role": "user", "content": "hi"}])
            assert exc_info.value.reason == "no_keys"

    asyncio.run(run())


def test_chat_429_wait0_raises_no_llm_available(tmp_path):
    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path), secrets=_secrets(), store=InMemoryStore()
        ) as broker:
            with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_error(429)):
                with pytest.raises(NoLLMAvailableError):
                    await broker.chat([{"role": "user", "content": "hi"}], wait=0)

    asyncio.run(run())


def test_chat_429_increments_fail_count(tmp_path):
    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path), secrets=_secrets(), store=InMemoryStore()
        ) as broker:
            with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_error(429)):
                with pytest.raises(NoLLMAvailableError):
                    await broker.chat([{"role": "user", "content": "hi"}], wait=0)
            state = await (await broker.get("p1")).state()
            assert state.fail_count == 1
            assert state.phase is LifecyclePhase.COOLING

    asyncio.run(run())


def test_chat_500_wait0_raises_no_llm_available(tmp_path):
    """A generic HTTP error cools the slot and fails over instead of raising immediately;
    with wait=0 and no other LLM to fail over to, that surfaces as NoLLMAvailableError."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path), secrets=_secrets(), store=InMemoryStore()
        ) as broker:
            with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_error(500)):
                with pytest.raises(NoLLMAvailableError):
                    await broker.chat([{"role": "user", "content": "hi"}], wait=0)

    asyncio.run(run())


def test_chat_empty_pool_wait0_raises_no_llm_available(tmp_path):
    """An empty registry now fails fast at provision — no LLMs to route to either way."""

    async def run():
        f = tmp_path / "empty.toml"
        f.write_text("")
        async with AsyncBroker(registry=FileRegistry(f), store=InMemoryStore()) as broker:
            with pytest.raises(NoLLMAvailableError):
                await broker.chat([{"role": "user", "content": "hi"}], wait=0)

    with pytest.raises(EmptyRegistryError, match="sync"):
        asyncio.run(run())


def test_empty_registry_error_propagates_out_of_host_entry_points(tmp_path):
    """The host boundary must be able to catch it: both inspection entry points
    provision lazily, so both surface the typed error rather than a bare one."""

    async def run():
        f = tmp_path / "empty.toml"
        f.write_text("")
        broker = AsyncBroker(registry=FileRegistry(f), store=InMemoryStore())
        with pytest.raises(EmptyRegistryError):
            await broker.count()
        with pytest.raises(EmptyRegistryError):
            await broker.snapshot()
        await broker.aclose()

    asyncio.run(run())


def test_stats_on_empty_registry_returns_empty_mapping_without_provisioning(tmp_path):
    """Journal reads never provision: a visibility call must survive an install whose
    registry is empty or stale — precisely the state a host UI most needs to render."""

    async def run():
        f = tmp_path / "empty.toml"
        f.write_text("")
        broker = AsyncBroker(registry=FileRegistry(f), store=SqliteStore(str(tmp_path / "s.db")))
        assert await broker.stats() == {}
        assert await broker.calls(limit=10) == []
        assert broker._provisioned is False
        await broker.aclose()

    asyncio.run(run())


def test_stats_aggregates_recorded_calls_per_model(tmp_path):
    async def run():
        db = str(tmp_path / "b.db")
        async with AsyncBroker(
            registry=_registry(tmp_path), secrets=_secrets(), store=SqliteStore(db)
        ) as broker:
            with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_ok("hi")):
                await broker.chat([{"role": "user", "content": "x"}], operation="summarize")
                await broker.chat([{"role": "user", "content": "y"}], operation="summarize")
            stats = await broker.stats()
            entry = next(iter(stats.values()))
            assert entry.total == 2
            assert entry.by_status == {CallStatus.OK: 2}
            assert entry.last_status is CallStatus.OK
            assert entry.first_at is not None
            assert entry.last_at is not None

    asyncio.run(run())


def test_stats_ignores_quality_records(tmp_path):
    """A quality record carries status=None by construction; counting it would put a
    row with no status in the host's denominator."""

    async def run():
        db = str(tmp_path / "b.db")
        async with AsyncBroker(
            registry=_registry(tmp_path), secrets=_secrets(), store=SqliteStore(db)
        ) as broker:
            with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_ok("hi")):
                result = await broker.chat([{"role": "user", "content": "x"}])
                await result.record_quality(1.0)
            assert next(iter((await broker.stats()).values())).total == 1

    asyncio.run(run())


def test_stats_since_bounds_the_window(tmp_path):
    async def run():
        db = str(tmp_path / "b.db")
        async with AsyncBroker(
            registry=_registry(tmp_path), secrets=_secrets(), store=SqliteStore(db)
        ) as broker:
            with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_ok("hi")):
                await broker.chat([{"role": "user", "content": "x"}])
            assert await broker.stats(since=datetime.now(UTC) + timedelta(days=1)) == {}
            assert await broker.stats(since=datetime.now(UTC) - timedelta(days=7)) != {}

    asyncio.run(run())


def test_stats_operation_filter_excludes_other_operations(tmp_path):
    async def run():
        db = str(tmp_path / "b.db")
        async with AsyncBroker(
            registry=_registry(tmp_path), secrets=_secrets(), store=SqliteStore(db)
        ) as broker:
            with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_ok("hi")):
                await broker.chat([{"role": "user", "content": "x"}], operation="summarize")
                await broker.chat([{"role": "user", "content": "y"}], operation="translate")
            assert next(iter((await broker.stats()).values())).total == 2
            summarize = await broker.stats(operation="summarize")
            assert next(iter(summarize.values())).total == 1

    asyncio.run(run())


def test_stats_on_the_default_file_store(tmp_path):
    """The default store for a TOML registry is FileStore, whose day-file journal is
    a separate read path from the SQL backends the other stats tests cover."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path),
            secrets=_secrets(),
            store=FileStore(tmp_path / "store"),
        ) as broker:
            with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_ok("hi")):
                await broker.ask("x", operation="summarize")
                await broker.ask("y", operation="summarize")
            stats = await broker.stats(since=datetime.now(UTC) - timedelta(days=1))
            assert stats["p1"].total == 2
            assert stats["p1"].by_status == {CallStatus.OK: 2}
            assert await broker.stats(operation="translate") == {}

    asyncio.run(run())


def test_stats_rejects_naive_since(tmp_path):
    async def run():
        broker = AsyncBroker(registry=_registry(tmp_path), store=FileStore(tmp_path / "store"))
        with pytest.raises(ValueError, match="timezone-aware"):
            await broker.stats(since=datetime(2030, 1, 1))  # noqa: DTZ001
        await broker.aclose()

    asyncio.run(run())


def test_journal_read_contract_holds_for_a_third_party_store(tmp_path):
    """The bound/limit contract is a promise of the public API, so it must not
    depend on the store backend upholding it — a host's own QueryableStoreProtocol
    implementation must never be handed a naive bound or a non-positive limit."""

    class _RecordingStore:
        def __init__(self):
            self.seen: list[dict] = []

        async def record(self, call):
            return

        async def record_quality(self, *a, **kw):
            return

        async def calls(self, **kw):
            self.seen.append(kw)
            return []

    async def run():
        store = _RecordingStore()
        broker = AsyncBroker(registry=_registry(tmp_path), store=store)
        with pytest.raises(ValueError, match="limit must be"):
            await broker.calls(limit=0)
        with pytest.raises(ValueError, match="timezone-aware"):
            await broker.stats(since=datetime(2030, 1, 1))  # noqa: DTZ001
        assert store.seen == []
        await broker.aclose()

    asyncio.run(run())


def test_result_record_quality_does_not_raise(tmp_path):
    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path), secrets=_secrets(), store=InMemoryStore()
        ) as broker:
            with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_ok("hi")):
                result = await broker.chat([{"role": "user", "content": "x"}])
                await result.record_quality(1.0)

    asyncio.run(run())


def test_result_record_quality_updates_the_window_instantly(tmp_path):
    """A host's own rating applies before any rebuild, whichever entry point it
    arrives through — the live result handle included."""

    async def run():
        opt = Optimizer(quality_min_count=10, quality_floor=0.3)
        async with AsyncBroker(
            registry=_registry(tmp_path),
            secrets=_secrets(),
            store=InMemoryStore(),
            optimize=opt,
        ) as broker:
            with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_ok("hi")):
                for _ in range(10):
                    result = await broker.chat(
                        [{"role": "user", "content": "x"}], operation="summarize"
                    )
                    await result.record_quality(0.0)
            assert opt.is_demoted("p1", "summarize") is True

    asyncio.run(run())


def test_result_exposes_rating_identity(tmp_path):
    """The result carries the identity a host must persist to rate the call later —
    and its call_id is the id of the call journal row."""

    async def run():
        db = str(tmp_path / "b.db")
        async with AsyncBroker(
            registry=_registry(tmp_path), secrets=_secrets(), store=SqliteStore(db)
        ) as broker:
            with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_ok("hi")):
                result = await broker.ask("x", operation="summarize")
            assert result.llm_name == "p1"
            assert result.operation == "summarize"
            assert isinstance(result.call_id, str) and result.call_id
            calls = await broker.calls(limit=10)
            call_rows = [c for c in calls if c.kind == "call"]
            assert call_rows[0].id == result.call_id

    asyncio.run(run())


def test_broker_record_quality_appends_self_contained_row(tmp_path):
    """Delayed record_quality through the broker appends its own quality journal row —
    identity supplied by the host, call row untouched."""

    async def run():
        db = str(tmp_path / "b.db")
        async with AsyncBroker(
            registry=_registry(tmp_path), secrets=_secrets(), store=SqliteStore(db)
        ) as broker:
            with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_ok("hi")):
                result = await broker.ask("x", operation="summarize")
            await broker.record_quality("p1", "summarize", 0.8, call_id=result.call_id)

            rows = await broker.calls(limit=10)
            quality_rows = [r for r in rows if r.kind == "quality"]
            assert len(quality_rows) == 1
            assert quality_rows[0].llm_name == "p1"
            assert quality_rows[0].operation == "summarize"
            assert quality_rows[0].quality_score == pytest.approx(0.8)
            assert quality_rows[0].call_id == result.call_id
            assert quality_rows[0].status is None

    asyncio.run(run())


def test_broker_record_quality_drives_demotion(tmp_path):
    """Enough zero scores fed through the delayed entry point demote the bucket —
    a delayed rating drives learning exactly as a live one would."""

    async def run():
        opt = Optimizer(quality_min_count=10, quality_floor=0.3)
        async with AsyncBroker(
            registry=_registry(tmp_path),
            secrets=_secrets(),
            store=InMemoryStore(),
            optimize=opt,
        ) as broker:
            for _ in range(10):
                await broker.record_quality("p1", "summarize", 0.0)
            assert opt.is_demoted("p1", "summarize") is True

    asyncio.run(run())


def test_broker_record_quality_survives_journal_rebuild(tmp_path):
    """A delayed rating is persisted, so demotion is re-derived from the journal on the
    next rebuild — not merely held in the in-memory window."""

    async def run():
        db = str(tmp_path / "b.db")
        opt = Optimizer(quality_min_count=10, quality_floor=0.3)
        async with AsyncBroker(
            registry=_registry(tmp_path),
            secrets=_secrets(),
            store=SqliteStore(db),
            optimize=opt,
        ) as broker:
            for _ in range(10):
                await broker.record_quality("p1", "summarize", 0.0)
            # Drop the in-memory window so only the persisted journal rows remain.
            opt.load_scores({})
            assert opt.is_demoted("p1", "summarize") is False
            # The forced rebuild re-derives the verdict purely from the quality rows.
            await broker._learner.maybe_rebuild(force=True)
            assert opt.is_demoted("p1", "summarize") is True

    asyncio.run(run())


def test_broker_record_quality_stamps_scope(tmp_path):
    """A scoped broker stamps its scope on the delayed quality row, so the host-facing
    calls(scope=...) filter surfaces it."""

    async def run():
        db = str(tmp_path / "b.db")
        await SqliteRegistry(db).mirror(
            [LLMConfig(name="p1", base_url="https://x/v1", model="m", api_key_ref="K")],
        )
        async with AsyncBroker(
            registry=SqliteRegistry(db),
            secrets=DictSecrets({"K": "test"}),
            store=SqliteStore(db),
            scope="alice",
        ) as broker:
            await broker.record_quality("p1", "summarize", 0.0)
            rows = await broker._store.calls(limit=10, scope="alice")
            quality_rows = [r for r in rows if r.kind == "quality"]
            assert len(quality_rows) == 1
            assert quality_rows[0].scope == "alice"

    asyncio.run(run())


def test_broker_record_quality_with_optimizer_off_does_not_raise(tmp_path):
    """optimize=False: record_quality still appends to the journal and does not raise."""

    async def run():
        db = str(tmp_path / "b.db")
        async with AsyncBroker(
            registry=_registry(tmp_path),
            secrets=_secrets(),
            store=SqliteStore(db),
            optimize=False,
        ) as broker:
            await broker.record_quality("p1", "summarize", 0.0)
            rows = await broker.calls(limit=10)
            assert [r for r in rows if r.kind == "quality"]

    asyncio.run(run())


def test_broker_record_quality_unknown_llm_does_not_raise(tmp_path):
    """Rating a model not in the pool is harmless — it appends a record nobody consults."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path), secrets=_secrets(), store=InMemoryStore()
        ) as broker:
            await broker.record_quality("ghost", "summarize", 0.0)

    asyncio.run(run())


def test_learning_does_not_make_a_non_queryable_store_look_queryable(tmp_path):
    """The broker holds the real backend, so the protocol question has an honest
    answer: an in-memory store cannot serve calls() however much learning is on."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path), secrets=_secrets(), store=InMemoryStore()
        ) as broker:
            assert broker._optimizer is not None  # learning is wired
            assert not isinstance(broker._store, QueryableStoreProtocol)
            with pytest.raises(TypeError, match="not queryable"):
                await broker.calls(limit=10)

    asyncio.run(run())


def test_sync_takes_a_curated_preset_name_and_nothing_else(tmp_path):
    """A path is not a sync source any more, and the refusal comes before the write,
    so the target keeps whatever it already had."""

    async def run():
        target = _registry(tmp_path).path
        original = target.read_text()
        broker = AsyncBroker(registry=FileRegistry(target), store=InMemoryStore())
        with pytest.raises(ValueError, match="unrecognized sync source"):
            await broker.sync(str(tmp_path / "other.toml"))
        assert target.read_text() == original

    asyncio.run(run())


def test_sync_into_a_registry_that_can_neither_be_written_nor_named_raises(tmp_path, monkeypatch):
    """A read-only registry object has no file to rewrite and no mirror to call."""
    _serve(monkeypatch)

    class _ReadOnly:
        async def load(self):
            return []

    async def run():
        broker = AsyncBroker(registry=_ReadOnly(), store=InMemoryStore())
        with pytest.raises(TypeError, match="does not support mutations"):
            await broker.sync("freetier")

    asyncio.run(run())


def test_calls_without_queryable_store_raises(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), store=InMemoryStore()) as broker:
            with pytest.raises(TypeError, match="queryable"):
                await broker.calls(limit=10)

    asyncio.run(run())


def test_snapshot_returns_entry_per_llm(tmp_path):
    async def run():
        entries = [("a", "https://a/v1", "m", "K"), ("b", "https://b/v1", "m", "K")]
        async with AsyncBroker(
            registry=_registry(tmp_path, entries), store=InMemoryStore()
        ) as broker:
            snap = await broker.snapshot()
            assert set(snap) == {"a", "b"}
            assert snap["a"].config.name == "a"

    asyncio.run(run())


def test_snapshot_carries_raw_facts_no_status_enum(tmp_path):
    """LLMSnapshot exposes disabled/has_key/cooldown_until/demoted_operations directly —
    no LifecyclePhase/status enum, no precedence rule."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path),
            secrets=_secrets(),
            store=InMemoryStore(),
        ) as broker:
            snap = await broker.snapshot()
            entry = snap["p1"]
            assert entry.disabled is False
            assert entry.has_key is True
            assert entry.cooldown_until is None
            assert entry.demoted_operations == ()

            await broker.disable_llm("p1")
            snap2 = await broker.snapshot()
            assert snap2["p1"].disabled is True

    asyncio.run(run())


# ── sync(): mirror semantics, empty-registry fail-fast ───────────────────────


def _serve(monkeypatch, model="m"):
    """Serve a one-entry curated lineup to ``sync("freetier")``."""
    monkeypatch.setattr(
        presets,
        "fetch_preset_text",
        lambda _name: (
            f'[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="{model}"\napi_key_ref="K"\n'
        ),
    )


def test_sync_populates_a_fresh_db_registry(tmp_path, monkeypatch):
    _serve(monkeypatch)

    async def run():
        db = str(tmp_path / "b.db")
        broker = AsyncBroker(
            registry=SqliteRegistry(db),
            store=InMemoryStore(),
        )
        await broker.sync("freetier")
        async with broker:
            assert (await broker.get("p1")).config.name == "p1"

    asyncio.run(run())


def test_sync_is_idempotent_no_extra_warnings(tmp_path, caplog, monkeypatch):
    """Calling sync() twice with the same preset is a no-op the second time."""
    _serve(monkeypatch)

    async def run():
        db = str(tmp_path / "b.db")
        broker = AsyncBroker(
            registry=SqliteRegistry(db),
            store=InMemoryStore(),
        )
        await broker.sync("freetier")
        caplog.clear()
        await broker.sync("freetier")

    with caplog.at_level(logging.WARNING, logger="llmbroker.broker"):
        asyncio.run(run())
    assert caplog.records == []


def test_sync_reconciles_registry_to_preset(tmp_path, monkeypatch):
    """sync() mirrors: adds new, updates existing, deletes entries absent from the preset."""
    _serve(monkeypatch)

    async def run():
        db = str(tmp_path / "b.db")
        sqlite_reg = SqliteRegistry(db)
        extra = LLMConfig(name="extra", base_url="https://e/v1", model="m", api_key_ref="K")
        await sqlite_reg.mirror([extra])

        broker = AsyncBroker(registry=sqlite_reg, store=InMemoryStore())
        await broker.sync("freetier")
        async with broker:
            assert (await broker.get("p1")).config.name == "p1"
            with pytest.raises(KeyError):
                await broker.get("extra")

    asyncio.run(run())


def test_sync_refuses_model_identity_change(tmp_path, monkeypatch):
    _serve(monkeypatch, model="model-b")

    async def run():
        db = str(tmp_path / "b.db")
        sqlite_reg = SqliteRegistry(db)
        await sqlite_reg.mirror(
            [LLMConfig(name="p1", base_url="https://x/v1", model="model-a", api_key_ref="K")],
        )
        broker = AsyncBroker(registry=sqlite_reg, store=InMemoryStore())
        with pytest.raises(ValueError, match="model-a"):
            await broker.sync("freetier")

    asyncio.run(run())


# ── scoping: registry is global, only secrets are per-scope ─────────────────


def test_registry_is_global_regardless_of_scope(tmp_path):
    """Two brokers with different scope over one sqlite registry see the same models —
    the registry has no per-scope partitioning: it is always global."""

    async def run():
        db = str(tmp_path / "b.db")
        reg = SqliteRegistry(db)
        await reg.mirror(
            [LLMConfig(name="llm", base_url="https://a/v1", model="m", api_key_ref="K")]
        )

        broker_a = AsyncBroker(registry=SqliteRegistry(db), scope="alice", store=InMemoryStore())
        broker_b = AsyncBroker(registry=SqliteRegistry(db), scope="bob", store=InMemoryStore())
        async with broker_a, broker_b:
            assert (await broker_a.get("llm")).config.base_url == "https://a/v1"
            assert (await broker_b.get("llm")).config.base_url == "https://a/v1"

    asyncio.run(run())


def test_scope_none_reproduces_single_tenant_behavior(tmp_path):
    """scope=None (default) is equivalent to single-tenant behavior."""

    async def run():
        db = str(tmp_path / "b.db")
        reg = SqliteRegistry(db)
        await reg.mirror(
            [LLMConfig(name="p1", base_url="https://x/v1", model="m", api_key_ref="K")]
        )

        async with AsyncBroker(registry=SqliteRegistry(db), store=InMemoryStore()) as broker:
            assert (await broker.get("p1")).config.name == "p1"

    asyncio.run(run())


def test_two_scopes_have_isolated_secrets(tmp_path):
    """Two brokers with different scope resolve different values for the same api_key_ref,
    via the scope-prefixed ref (own key), falling back to the shared ref."""

    async def run():
        db = str(tmp_path / "b.db")
        secrets = SqliteSecrets(db)
        await secrets.set("alice/KEY", "alice-secret")
        await secrets.set("bob/KEY", "bob-secret")

        reg = SqliteRegistry(db)
        await reg.mirror(
            [LLMConfig(name="llm", base_url="https://x/v1", model="m", api_key_ref="KEY")]
        )

        broker_a = AsyncBroker(
            registry=SqliteRegistry(db),
            secrets=secrets,
            scope="alice",
            store=InMemoryStore(),
        )
        broker_b = AsyncBroker(
            registry=SqliteRegistry(db),
            secrets=secrets,
            scope="bob",
            store=InMemoryStore(),
        )
        async with broker_a, broker_b:
            assert broker_a._pool.resolved_key("llm") == "alice-secret"
            assert broker_b._pool.resolved_key("llm") == "bob-secret"

    asyncio.run(run())


def test_scope_without_own_key_falls_back_to_shared_ref(tmp_path):
    """A scope with no own-prefixed secret falls back to the shared (unprefixed) ref."""

    async def run():
        db = str(tmp_path / "b.db")
        secrets = SqliteSecrets(db)
        await secrets.set("KEY", "shared-secret")

        reg = SqliteRegistry(db)
        await reg.mirror(
            [LLMConfig(name="llm", base_url="https://x/v1", model="m", api_key_ref="KEY")]
        )

        async with AsyncBroker(
            registry=SqliteRegistry(db),
            secrets=secrets,
            scope="alice",
            store=InMemoryStore(),
        ) as broker:
            assert broker._pool.resolved_key("llm") == "shared-secret"

    asyncio.run(run())


# ── default store wiring (no explicit store=) ────────────────────────


def test_default_store_is_file_store_inside_the_home_directory(tmp_path):
    """A zero-config broker with no explicit store= keeps its journal in a
    `store/` dir inside its own home."""
    _registry(tmp_path, filename="lineup.toml")

    async def run():
        async with AsyncBroker(home=tmp_path, sync=None) as broker:
            await broker._store.record_quality("p1", None, 1.0)

    asyncio.run(run())
    assert (tmp_path / "store").is_dir()
    assert list((tmp_path / "store" / "calls").glob("*.jsonl"))


def test_default_store_falls_back_to_cwd_store_for_bare_db_registry(tmp_path, monkeypatch):
    """A registry with no `.path` (e.g. a bare DB registry object, not a source
    string) falls back to `./store` under the CWD — not an error, just an
    unopinionated default."""
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "b.db")

    async def run():
        reg = SqliteRegistry(db)
        await reg.mirror(
            [LLMConfig(name="p1", base_url="https://x/v1", model="m", api_key_ref="K")]
        )
        async with AsyncBroker(registry=SqliteRegistry(db)) as broker:
            await broker._store.record_quality("p1", None, 1.0)

    asyncio.run(run())
    assert (tmp_path / "store").is_dir()
    assert list((tmp_path / "store" / "calls").glob("*.jsonl"))


def test_sqlite_source_default_store_is_sqlite_store(tmp_path):
    """A ``.db`` source with no explicit store= wires a sqlite.Store, not
    the file-registry ``store/`` sibling default."""

    async def run():
        db_path = str(tmp_path / "broker.db")
        await SqliteRegistry(db_path).mirror(
            [LLMConfig(name="p1", base_url="https://x/v1", model="m", api_key_ref="K")]
        )
        async with AsyncBroker(db_path) as broker:
            assert isinstance(broker._store, DriverStore)

    asyncio.run(run())
