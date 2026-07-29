"""Tests for the synchronous Broker / LLM / Result wrappers."""

import asyncio
import gc
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.models import LifecyclePhase, LLMConfig
from llmbroker.sqlite import Store as SqliteStore
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.standalone.registry import Registry as FileRegistry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.sync import Broker
from llmbroker.standalone.store import InMemoryStore


def _registry(tmp_path, name="p1"):
    f = tmp_path / "llms.toml"
    f.write_text(f'[[llms]]\nname="{name}"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')
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


def test_broker_count(tmp_path):
    with Broker(registry=_registry(tmp_path), store=InMemoryStore()) as broker:
        assert broker.count() == 1


def test_broker_snapshot_names(tmp_path):
    with Broker(registry=_registry(tmp_path), store=InMemoryStore()) as broker:
        assert list(broker.snapshot().keys()) == ["p1"]


def test_broker_get_returns_llm(tmp_path):
    with Broker(registry=_registry(tmp_path), store=InMemoryStore()) as broker:
        llm = broker.get("p1")
        assert llm.config.name == "p1"


def test_broker_get_missing_raises(tmp_path):
    with Broker(registry=_registry(tmp_path), store=InMemoryStore()) as broker:
        with pytest.raises(KeyError):
            broker.get("nope")


def _secrets() -> DictSecrets:
    return DictSecrets({"K": "test"})


def test_broker_chat_happy_path(tmp_path):
    with Broker(registry=_registry(tmp_path), secrets=_secrets(), store=InMemoryStore()) as broker:
        with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_ok("sync-hello")):
            result = broker.chat([{"role": "user", "content": "hi"}])
            assert result.text == "sync-hello"


def test_broker_ask_happy_path(tmp_path):
    with Broker(registry=_registry(tmp_path), secrets=_secrets(), store=InMemoryStore()) as broker:
        with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_ok("yes")):
            result = broker.ask("question")
            assert result.text == "yes"


def test_broker_chat_500_wait0_raises_no_llm_available(tmp_path):
    """A generic HTTP error cools the slot and fails over instead of raising immediately;
    with wait=0 and no other LLM to fail over to, that surfaces as NoLLMAvailableError."""
    with Broker(registry=_registry(tmp_path), secrets=_secrets(), store=InMemoryStore()) as broker:
        with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_error(500)):
            with pytest.raises(NoLLMAvailableError):
                broker.chat([{"role": "user", "content": "hi"}], wait=0)


def test_result_record_quality_does_not_raise(tmp_path):
    with Broker(registry=_registry(tmp_path), secrets=_secrets(), store=InMemoryStore()) as broker:
        with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_ok("hi")):
            result = broker.chat([{"role": "user", "content": "x"}])
            result.record_quality(1.0)


def test_result_exposes_rating_identity(tmp_path):
    """The sync result mirrors the async identity properties."""
    with Broker(registry=_registry(tmp_path), secrets=_secrets(), store=InMemoryStore()) as broker:
        with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_ok("hi")):
            result = broker.ask("x", operation="summarize")
        assert result.llm_name == "p1"
        assert result.operation == "summarize"
        assert isinstance(result.call_id, str) and result.call_id


def test_broker_record_quality_delayed(tmp_path):
    """Sync Broker.record_quality records a delayed rating from persisted identity alone —
    no live result object required."""
    db = str(tmp_path / "b.db")
    with Broker(registry=_registry(tmp_path), secrets=_secrets(), store=SqliteStore(db)) as broker:
        with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_ok("hi")):
            result = broker.ask("x", operation="summarize")
        broker.record_quality(result.llm_name, "summarize", 0.9, call_id=result.call_id)
        quality_rows = [r for r in broker.calls(limit=10) if r.kind == "quality"]
        assert len(quality_rows) == 1
        assert quality_rows[0].llm_name == "p1"
        assert quality_rows[0].call_id == result.call_id


def test_broker_stats_mirrors_the_async_aggregate(tmp_path):
    db = str(tmp_path / "b.db")
    with Broker(registry=_registry(tmp_path), secrets=_secrets(), store=SqliteStore(db)) as broker:
        with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_ok("hi")):
            broker.ask("x", operation="summarize")
            broker.ask("y", operation="summarize")
        assert broker.stats()["p1"].total == 2
        assert broker.stats(operation="translate") == {}


def test_broker_calls_accepts_the_narrowing_filters(tmp_path):
    db = str(tmp_path / "b.db")
    with Broker(registry=_registry(tmp_path), secrets=_secrets(), store=SqliteStore(db)) as broker:
        with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_ok("hi")):
            result = broker.ask("x", operation="summarize")
        broker.record_quality(result.llm_name, "summarize", 0.9)
        assert [r.kind for r in broker.calls(limit=10, kind="quality")] == ["quality"]
        assert [r.kind for r in broker.calls(limit=10, kind="call")] == ["call"]
        assert broker.calls(limit=10, since=datetime.now(UTC) + timedelta(days=1)) == []


def test_llm_state_is_available(tmp_path):
    with Broker(registry=_registry(tmp_path), store=InMemoryStore()) as broker:
        llm = broker.get("p1")
        assert llm.state().phase is LifecyclePhase.AVAILABLE


def test_broker_context_manager_closes_cleanly(tmp_path):
    broker = Broker(registry=_registry(tmp_path), store=InMemoryStore())
    with broker:
        _ = broker.count()
    assert not broker._finalizer.alive
    assert not broker._thread.is_alive()


def test_broker_gc_stops_thread_without_close(tmp_path):
    """An abandoned Broker reclaims its loop thread when garbage-collected."""
    broker = Broker(registry=_registry(tmp_path), store=InMemoryStore())
    thread = broker._thread
    assert thread.is_alive()
    del broker
    gc.collect()
    thread.join(timeout=5.0)
    assert not thread.is_alive()


def test_broker_disable_llm_benches_and_excludes_from_pool(tmp_path):
    db = str(tmp_path / "b.db")
    asyncio.run(
        SqliteRegistry(db).mirror(
            [LLMConfig(name="p1", base_url="https://x/v1", model="m", api_key_ref="K")],
        )
    )
    with Broker(registry=SqliteRegistry(db), store=InMemoryStore()) as broker:
        broker.disable_llm("p1")
        assert broker._async._pool.is_disabled("p1")


def test_broker_enable_llm_readmits_after_disable(tmp_path):
    db = str(tmp_path / "b.db")
    asyncio.run(
        SqliteRegistry(db).mirror(
            [LLMConfig(name="p1", base_url="https://x/v1", model="m", api_key_ref="K")],
        )
    )
    with Broker(registry=SqliteRegistry(db), store=InMemoryStore()) as broker:
        broker.disable_llm("p1")
        broker.enable_llm("p1")
        assert not broker._async._pool.is_disabled("p1")


def test_llm_disabled_property_round_trips_via_get(tmp_path):
    db = str(tmp_path / "b.db")
    asyncio.run(
        SqliteRegistry(db).mirror(
            [LLMConfig(name="p1", base_url="https://x/v1", model="m", api_key_ref="K")],
        )
    )
    with Broker(registry=SqliteRegistry(db), store=InMemoryStore()) as broker:
        assert broker.get("p1").disabled is False
        broker.disable_llm("p1")
        assert broker.get("p1").disabled is True
        broker.enable_llm("p1")
        assert broker.get("p1").disabled is False


def test_broker_disable_llm_persists_to_store_disabled_map(tmp_path):
    db = str(tmp_path / "b.db")
    asyncio.run(
        SqliteRegistry(db).mirror(
            [LLMConfig(name="p1", base_url="https://x/v1", model="m", api_key_ref="K")],
        )
    )
    with Broker(
        registry=SqliteRegistry(db),
        store=SqliteStore(db),
    ) as broker:
        broker.disable_llm("p1")

    async def read_back():
        return await SqliteStore(db).get_disabled("p1")

    assert asyncio.run(read_back()) is True


# ── sync() ────────────────────────────────────────────────────────────────


def _seed_registry(tmp_path, name="p1"):
    f = tmp_path / "seed.toml"
    f.write_text(f'[[llms]]\nname="{name}"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')
    return FileRegistry(f)


def test_broker_sync_populates_a_fresh_db(tmp_path):
    db = str(tmp_path / "b.db")
    broker = Broker(registry=SqliteRegistry(db), store=InMemoryStore())
    broker.sync(_seed_registry(tmp_path))
    with broker:
        assert broker.count() == 1
        assert broker.get("p1").config.name == "p1"


def test_broker_sync_reconciles_registry_to_preset(tmp_path):
    """sync() mirrors: adds new, updates existing, deletes entries absent from the preset."""
    db = str(tmp_path / "b.db")
    extra = LLMConfig(name="extra", base_url="https://e/v1", model="m", api_key_ref="K")
    asyncio.run(SqliteRegistry(db).mirror([extra]))

    broker = Broker(registry=SqliteRegistry(db), store=InMemoryStore())
    broker.sync(_seed_registry(tmp_path))
    with broker:
        assert broker.get("p1").config.name == "p1"
        with pytest.raises(KeyError):
            broker.get("extra")


def test_broker_sync_with_readonly_source_registry_raises(tmp_path):
    broker = Broker(registry=_registry(tmp_path), store=InMemoryStore())
    with pytest.raises(TypeError, match="does not support mutations"):
        broker.sync(_seed_registry(tmp_path))


def test_broker_scope_forwarded_to_async_broker(tmp_path):
    """Broker(scope=...) forwards scope to the underlying AsyncBroker."""
    db = str(tmp_path / "b.db")
    broker = Broker(
        registry=SqliteRegistry(db),
        scope="alice",
        store=InMemoryStore(),
    )
    try:
        assert broker._async._scope == "alice"
    finally:
        broker.close()
