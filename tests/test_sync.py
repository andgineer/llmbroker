"""Tests for the synchronous Broker / LLM / Result wrappers."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from llmbroker.broker import AllLLMsFailedError
from llmbroker.registry import Registry as FileRegistry
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


def test_broker_chat_happy_path(tmp_path):
    with Broker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
        with patch("llmbroker.broker.httpx.AsyncClient", return_value=_http_ok("sync-hello")):
            result = broker.chat([{"role": "user", "content": "hi"}])
            assert result.text == "sync-hello"


def test_broker_ask_happy_path(tmp_path):
    with Broker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
        with patch("llmbroker.broker.httpx.AsyncClient", return_value=_http_ok("yes")):
            result = broker.ask("question")
            assert result.text == "yes"


def test_broker_chat_500_raises_all_failed(tmp_path):
    with Broker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
        with patch("llmbroker.broker.httpx.AsyncClient", return_value=_http_error(500)):
            with pytest.raises(AllLLMsFailedError):
                broker.chat([{"role": "user", "content": "hi"}])


def test_result_record_quality_does_not_raise(tmp_path):
    with Broker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
        with patch("llmbroker.broker.httpx.AsyncClient", return_value=_http_ok("hi")):
            result = broker.chat([{"role": "user", "content": "x"}])
            result.record_quality(1.0)


def test_llm_state_is_available(tmp_path):
    from llmbroker.models import LifecyclePhase

    with Broker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
        llm = broker["p1"]
        assert llm.state().phase is LifecyclePhase.AVAILABLE


def test_broker_context_manager_closes_cleanly(tmp_path):
    broker = Broker(registry=_registry(tmp_path), telemetry=NoTelemetry())
    with broker:
        _ = len(broker)
    assert broker._closed
