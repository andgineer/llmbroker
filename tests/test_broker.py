"""Tests for AsyncBroker core routing, sync(), error escalation."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from llmbroker.backends.ports import DriverStore
from llmbroker.broker.broker import AsyncBroker
from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.models import LifecyclePhase, LLMConfig
from llmbroker.optimizer import Optimizer
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.sqlite import Secrets as SqliteSecrets
from llmbroker.sqlite import Store as SqliteStore
from llmbroker.standalone.registry import Registry as FileRegistry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import InMemoryStore


def _registry(tmp_path, entries=None):
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
    f = tmp_path / "llms.toml"
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

    with pytest.raises(RuntimeError, match="sync"):
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


def test_sync_with_readonly_source_registry_raises(tmp_path):
    """sync() into a read-only (file) registry raises — a mutable registry is required."""

    async def run():
        other = tmp_path / "other.toml"
        other.write_text(
            '[[llms]]\nname="p2"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n'
        )
        broker = AsyncBroker(registry=_registry(tmp_path), store=InMemoryStore())
        with pytest.raises(TypeError, match="does not support mutations"):
            await broker.sync(FileRegistry(other))

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


def _toml_registry(tmp_path, name="p1"):
    f = tmp_path / "seed.toml"
    f.write_text(f'[[llms]]\nname="{name}"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')
    return FileRegistry(f)


def test_sync_populates_a_fresh_db_registry(tmp_path):
    async def run():
        db = str(tmp_path / "b.db")
        broker = AsyncBroker(
            registry=SqliteRegistry(db),
            store=InMemoryStore(),
        )
        await broker.sync(_toml_registry(tmp_path))
        async with broker:
            assert (await broker.get("p1")).config.name == "p1"

    asyncio.run(run())


def test_sync_is_idempotent_no_extra_warnings(tmp_path, caplog):
    """Calling sync() twice with the same preset is a no-op the second time."""

    async def run():
        db = str(tmp_path / "b.db")
        broker = AsyncBroker(
            registry=SqliteRegistry(db),
            store=InMemoryStore(),
        )
        await broker.sync(_toml_registry(tmp_path))
        caplog.clear()
        await broker.sync(_toml_registry(tmp_path))

    with caplog.at_level(logging.WARNING, logger="llmbroker.broker"):
        asyncio.run(run())
    assert caplog.records == []


def test_sync_reconciles_registry_to_preset(tmp_path):
    """sync() mirrors: adds new, updates existing, deletes entries absent from the preset."""

    async def run():
        db = str(tmp_path / "b.db")
        sqlite_reg = SqliteRegistry(db)
        extra = LLMConfig(name="extra", base_url="https://e/v1", model="m", api_key_ref="K")
        await sqlite_reg.mirror([extra])

        broker = AsyncBroker(registry=sqlite_reg, store=InMemoryStore())
        await broker.sync(_toml_registry(tmp_path))
        async with broker:
            assert (await broker.get("p1")).config.name == "p1"
            with pytest.raises(KeyError):
                await broker.get("extra")

    asyncio.run(run())


def test_sync_refuses_model_identity_change(tmp_path):
    async def run():
        db = str(tmp_path / "b.db")
        sqlite_reg = SqliteRegistry(db)
        await sqlite_reg.mirror(
            [LLMConfig(name="p1", base_url="https://x/v1", model="model-a", api_key_ref="K")],
        )
        broker = AsyncBroker(registry=sqlite_reg, store=InMemoryStore())
        preset = tmp_path / "preset.toml"
        preset.write_text(
            '[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="model-b"\napi_key_ref="K"\n',
        )
        with pytest.raises(ValueError, match="model-a"):
            await broker.sync(FileRegistry(preset))

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


def test_default_store_is_file_store_sibling_to_toml_registry(tmp_path):
    """A file/TOML registry with no explicit store= gets a FileStore in a
    `store/` dir sibling to the config file."""

    async def run():
        async with AsyncBroker(registry=_registry(tmp_path)) as broker:
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
            assert isinstance(broker._base_store, DriverStore)

    asyncio.run(run())
