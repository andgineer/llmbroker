"""E2E integration tests: AsyncBroker over real backend stacks.

Verifies port wiring and persistence across the backend boundary.
HTTP is mocked at llmbroker.chat.httpx.AsyncClient.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.models import Call, CallStatus, LLMConfig, LifecyclePhase
from llmbroker.protocols.registry import MutableRegistryProtocol
from llmbroker.protocols.secrets import MutableSecretsProtocol
from llmbroker.standalone.secrets import DictSecrets


# ---------------------------------------------------------------------------
# HTTP mock helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Config + seeding helpers
# ---------------------------------------------------------------------------


def _cfg(name: str, api_key_ref: str = "KEY") -> LLMConfig:
    return LLMConfig(
        name=name, base_url=f"https://{name}.example/v1", model="gpt-4o", api_key_ref=api_key_ref
    )


async def _seed_stack(stack, configs: list[LLMConfig], keys: dict[str, str], monkeypatch) -> None:
    """Populate registry and secrets for any stack variant before the broker opens."""
    if isinstance(stack.registry, MutableRegistryProtocol):
        for cfg in configs:
            await stack.registry.add(cfg)
    else:
        lines = []
        for cfg in configs:
            lines += [
                "[[llms]]",
                f'name="{cfg.name}"',
                f'base_url="{cfg.base_url}"',
                f'model="{cfg.model}"',
                f'api_key_ref="{cfg.api_key_ref}"',
            ]
        stack.registry._path.write_text("\n".join(lines) + "\n")

    for ref, val in keys.items():
        if isinstance(stack.secrets, MutableSecretsProtocol):
            await stack.secrets.set(ref, val)
        elif isinstance(stack.secrets, DictSecrets):
            stack.secrets._mapping[ref] = val  # type: ignore[attr-defined]
        else:
            monkeypatch.setenv(ref, val)


# ---------------------------------------------------------------------------
# E1 — provision loads registry and resolves keys
# ---------------------------------------------------------------------------


async def test_provision_loads_registry_and_resolves_keys(stack, monkeypatch):
    """ensure_pool loads configs from registry and resolves API keys from secrets."""
    await _seed_stack(stack, [_cfg("llm1"), _cfg("llm2")], {"KEY": "test-key"}, monkeypatch)
    async with stack.make_broker() as broker:
        assert await broker.count() == 2
        assert broker._pool._resolved_keys.get("llm1") == "test-key"
        assert broker._pool._resolved_keys.get("llm2") == "test-key"


# ---------------------------------------------------------------------------
# E2 — warm-start primes delay from persisted calls
# ---------------------------------------------------------------------------


async def test_warm_start_primes_delay_from_persisted_calls(persistent_stack, monkeypatch):
    """Optimizer seeds max_delay from a persisted RATE_LIMITED metric on restart."""
    await _seed_stack(persistent_stack, [_cfg("llm1")], {"KEY": "test-key"}, monkeypatch)
    await persistent_stack.telemetry.record(
        Call(
            id=str(uuid.uuid4()),
            llm_name="llm1",
            operation=None,
            trace_id=None,
            status=CallStatus.RATE_LIMITED,
        )
    )
    async with persistent_stack.make_broker() as broker:
        assert broker._optimizer is not None
        assert broker._optimizer.delay_for("llm1") == broker._optimizer.max_delay


# ---------------------------------------------------------------------------
# E3 — cooldown state persists across restart
# ---------------------------------------------------------------------------


async def test_state_persists_across_restart(persistent_stack, monkeypatch):
    """Cooldown written by broker 1 is restored by broker 2 from the same state_store."""
    await _seed_stack(persistent_stack, [_cfg("llm1")], {"KEY": "test-key"}, monkeypatch)
    async with persistent_stack.make_broker() as broker1:
        with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_error(429)):
            with pytest.raises(NoLLMAvailableError):
                await broker1.chat([{"role": "user", "content": "hi"}], wait=0)
    async with persistent_stack.make_broker() as broker2:
        state = await (await broker2.get("llm1")).state()
        assert state.phase is LifecyclePhase.COOLING


# ---------------------------------------------------------------------------
# E4 — failover routes and journals
# ---------------------------------------------------------------------------


async def test_failover_routes_and_journals(stack, monkeypatch):
    """llm1→429 falls over to llm2→OK; queryable stacks record both calls."""
    await _seed_stack(stack, [_cfg("llm1"), _cfg("llm2")], {"KEY": "test-key"}, monkeypatch)
    async with stack.make_broker() as broker:
        with patch(
            "llmbroker.chat.httpx.AsyncClient",
            side_effect=[_http_error(429), _http_ok("ok-response")],
        ):
            result = await broker.chat([{"role": "user", "content": "hi"}])
        assert result.text == "ok-response"
        if stack.queryable:
            calls = await broker.calls(limit=10)
            statuses = {c.status for c in calls}
            assert CallStatus.RATE_LIMITED in statuses
            assert CallStatus.OK in statuses


# ---------------------------------------------------------------------------
# E5 — all offline raises and alerts
# ---------------------------------------------------------------------------


async def test_all_offline_raises_and_alerts(stack, monkeypatch):
    """All LLMs rate-limited → NoLLMAvailableError and an under-provision alert."""
    await _seed_stack(stack, [_cfg("llm1"), _cfg("llm2")], {"KEY": "test-key"}, monkeypatch)
    async with stack.make_broker() as broker:
        with patch(
            "llmbroker.chat.httpx.AsyncClient",
            side_effect=[_http_error(429), _http_error(429)],
        ):
            with pytest.raises(NoLLMAvailableError):
                await broker.chat([{"role": "user", "content": "hi"}], wait=0)
            with pytest.raises(NoLLMAvailableError):
                await broker.chat([{"role": "user", "content": "hi"}], wait=0)
        alerts = await broker.alerts()
        assert len(alerts) >= 1
        assert any("under-provisioned" in a.message for a in alerts)


# ---------------------------------------------------------------------------
# E6 — catalog mutation persists across rebuild
# ---------------------------------------------------------------------------


async def test_catalog_mutation_persists(stack, monkeypatch):
    """broker.add() persists via mutable registry; a rebuilt broker sees the entry."""
    if isinstance(stack.registry, MutableRegistryProtocol):
        await _seed_stack(stack, [], {"KEY": "test-key"}, monkeypatch)
        async with stack.make_broker() as broker1:
            await broker1.add(_cfg("llm1"))
        async with stack.make_broker() as broker2:
            assert await broker2.count() == 1
            assert (await broker2.get("llm1")).config.name == "llm1"
    else:
        async with stack.make_broker() as broker:
            assert await broker.count() == 0
