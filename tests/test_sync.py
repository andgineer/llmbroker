"""Tests for the synchronous Broker / LLM / Result wrappers."""

import asyncio
import gc
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import llmbroker.sqlite
import pytest

from llmbroker.broker import AllLLMsFailedError
from llmbroker.models import LifecyclePhase, LLMConfig, SeedPolicy
from llmbroker.registry import Registry as FileRegistry
from llmbroker.secrets import DictSecrets
from llmbroker.sync import Broker
from llmbroker.telemetry import NoTelemetry


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
    return cm


def test_broker_len(tmp_path):
    with Broker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
        assert len(broker) == 1


def test_broker_iter(tmp_path):
    with Broker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
        assert list(broker) == ["p1"]


def test_broker_getitem_returns_llm(tmp_path):
    with Broker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
        llm = broker["p1"]
        assert llm.config.name == "p1"


def test_broker_getitem_missing_raises(tmp_path):
    with Broker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
        with pytest.raises(KeyError):
            _ = broker["nope"]


def _secrets() -> DictSecrets:
    return DictSecrets({"K": "test"})


def test_broker_chat_happy_path(tmp_path):
    with Broker(
        registry=_registry(tmp_path), secrets=_secrets(), telemetry=NoTelemetry()
    ) as broker:
        with patch("llmbroker.broker.httpx.AsyncClient", return_value=_http_ok("sync-hello")):
            result = broker.chat([{"role": "user", "content": "hi"}])
            assert result.text == "sync-hello"


def test_broker_ask_happy_path(tmp_path):
    with Broker(
        registry=_registry(tmp_path), secrets=_secrets(), telemetry=NoTelemetry()
    ) as broker:
        with patch("llmbroker.broker.httpx.AsyncClient", return_value=_http_ok("yes")):
            result = broker.ask("question")
            assert result.text == "yes"


def test_broker_chat_500_raises_all_failed(tmp_path):
    with Broker(
        registry=_registry(tmp_path), secrets=_secrets(), telemetry=NoTelemetry()
    ) as broker:
        with patch("llmbroker.broker.httpx.AsyncClient", return_value=_http_error(500)):
            with pytest.raises(AllLLMsFailedError):
                broker.chat([{"role": "user", "content": "hi"}])


def test_result_record_quality_does_not_raise(tmp_path):
    with Broker(
        registry=_registry(tmp_path), secrets=_secrets(), telemetry=NoTelemetry()
    ) as broker:
        with patch("llmbroker.broker.httpx.AsyncClient", return_value=_http_ok("hi")):
            result = broker.chat([{"role": "user", "content": "x"}])
            result.record_quality(1.0)


def test_llm_state_is_available(tmp_path):
    with Broker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
        llm = broker["p1"]
        assert llm.state().phase is LifecyclePhase.AVAILABLE


def test_broker_context_manager_closes_cleanly(tmp_path):
    broker = Broker(registry=_registry(tmp_path), telemetry=NoTelemetry())
    with broker:
        _ = len(broker)
    assert not broker._finalizer.alive
    assert not broker._thread.is_alive()


def test_broker_gc_stops_thread_without_close(tmp_path):
    """An abandoned Broker reclaims its loop thread when garbage-collected."""
    broker = Broker(registry=_registry(tmp_path), telemetry=NoTelemetry())
    thread = broker._thread
    assert thread.is_alive()
    del broker
    gc.collect()
    thread.join(timeout=5.0)
    assert not thread.is_alive()


# ── constructor seed tests ────────────────────────────────────────────────────


def _seed_registry(tmp_path, name="p1"):
    f = tmp_path / "seed.toml"
    f.write_text(f'[[llms]]\nname="{name}"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')
    return FileRegistry(f)


def test_broker_enter_seeds_eagerly(tmp_path):
    """with Broker(..., seed=...) populates the pool before the body runs."""
    db = str(tmp_path / "b.db")
    with Broker(
        registry=llmbroker.sqlite.Registry(db),
        seed=_seed_registry(tmp_path),
        seed_policy=SeedPolicy.IF_EMPTY,
        telemetry=NoTelemetry(),
    ) as broker:
        assert "p1" in broker
        assert len(broker) == 1


def test_broker_seed_policy_add_preserves_existing(tmp_path):
    """seed_policy=ADD adds new entries but leaves existing ones untouched."""
    db = str(tmp_path / "b.db")
    extra = LLMConfig(name="extra", base_url="https://e/v1", model="m", api_key_ref="K")
    asyncio.run(llmbroker.sqlite.Registry(db).add(extra))

    with Broker(
        registry=llmbroker.sqlite.Registry(db),
        seed=_seed_registry(tmp_path),
        seed_policy=SeedPolicy.ADD,
        telemetry=NoTelemetry(),
    ) as broker:
        assert "p1" in broker
        assert "extra" in broker


def test_broker_seed_policy_mirror_reconciles(tmp_path):
    """seed_policy=MIRROR removes entries absent from seed on first enter."""
    db = str(tmp_path / "b.db")
    extra = LLMConfig(name="extra", base_url="https://e/v1", model="m", api_key_ref="K")
    asyncio.run(llmbroker.sqlite.Registry(db).add(extra))

    with Broker(
        registry=llmbroker.sqlite.Registry(db),
        seed=_seed_registry(tmp_path),
        seed_policy=SeedPolicy.MIRROR,
        telemetry=NoTelemetry(),
    ) as broker:
        assert "p1" in broker
        assert "extra" not in broker


def test_broker_seed_with_readonly_registry_raises(tmp_path):
    """Passing seed= with a read-only registry raises TypeError on enter."""
    with pytest.raises(TypeError, match="does not support mutations"):
        with Broker(
            registry=_registry(tmp_path),
            seed=_seed_registry(tmp_path),
            telemetry=NoTelemetry(),
        ):
            pass
