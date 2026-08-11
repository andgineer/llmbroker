"""E2E integration tests: AsyncBroker over real backend stacks.

Verifies port wiring and persistence across the backend boundary.
HTTP is mocked at llmbroker.chat.httpx.AsyncClient.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from llmbroker.exceptions import EmptyRegistryError, NoLLMAvailableError
from llmbroker.models import CallStatus, LLMConfig, LifecyclePhase
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


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    """A bare ``httpx.HTTPStatusError``, for patching ``call_provider`` directly —
    needed when a test's side effects must vary across attempts within one broker,
    since the broker now reuses a single lazily-created HTTP client per call."""
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.text = f"HTTP {status}"
    return httpx.HTTPStatusError("err", request=MagicMock(), response=resp)


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
        await stack.registry.mirror(configs)
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
        assert (
            await broker._shared_ring.resolve(broker._pool.config("llm1").api_key_ref) == "test-key"
        )
        assert (
            await broker._shared_ring.resolve(broker._pool.config("llm2").api_key_ref) == "test-key"
        )


# ---------------------------------------------------------------------------
# E3 — the journal survives a restart, availability does not
# ---------------------------------------------------------------------------


async def test_the_journal_survives_a_restart_and_availability_does_not(
    persistent_stack,
    monkeypatch,
):
    """The 429 row is durable — quality and the admin read derive from it. The
    cooldown it caused is not: it belonged to the process that met it (invariant 11)."""
    await _seed_stack(persistent_stack, [_cfg("llm1")], {"KEY": "test-key"}, monkeypatch)
    async with persistent_stack.make_broker() as broker1:
        with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_error(429)):
            with pytest.raises(NoLLMAvailableError):
                await broker1.chat([{"role": "user", "content": "hi"}], wait=0)
    async with persistent_stack.make_broker() as broker2:
        state = await (await broker2.get("llm1")).state()
        assert state.phase is LifecyclePhase.AVAILABLE
        if persistent_stack.queryable:
            rows = await broker2.calls(limit=10)
            assert [r.status for r in rows] == [CallStatus.RATE_LIMITED]


# ---------------------------------------------------------------------------
# E4 — failover routes and journals
# ---------------------------------------------------------------------------


async def test_failover_routes_and_journals(stack, monkeypatch):
    """llm1→429 falls over to llm2→OK; queryable stacks record both calls."""
    await _seed_stack(stack, [_cfg("llm1"), _cfg("llm2")], {"KEY": "test-key"}, monkeypatch)
    async with stack.make_broker() as broker:
        with patch(
            "llmbroker.broker.router.call_provider",
            new=AsyncMock(side_effect=[_http_status_error(429), ("ok-response", None, None)]),
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


async def test_all_offline_raises_and_alerts(stack, monkeypatch, caplog):
    """All LLMs rate-limited → NoLLMAvailableError and an under-provision log line."""
    await _seed_stack(stack, [_cfg("llm1"), _cfg("llm2")], {"KEY": "test-key"}, monkeypatch)
    async with stack.make_broker() as broker:
        with (
            patch(
                "llmbroker.broker.router.call_provider",
                new=AsyncMock(side_effect=[_http_status_error(429), _http_status_error(429)]),
            ),
            caplog.at_level("WARNING", logger="llmbroker.broker"),
        ):
            with pytest.raises(NoLLMAvailableError):
                await broker.chat([{"role": "user", "content": "hi"}], wait=0)
            with pytest.raises(NoLLMAvailableError):
                await broker.chat([{"role": "user", "content": "hi"}], wait=0)
        assert any("under-provisioned" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# E6 — catalog mutation persists across rebuild
# ---------------------------------------------------------------------------


async def test_catalog_mutation_persists(stack, monkeypatch):
    """broker.sync() persists via a mutable registry; a rebuilt broker sees the entry."""
    if isinstance(stack.registry, MutableRegistryProtocol):
        await stack.registry.mirror([_cfg("llm1")])
        for ref, val in {"KEY": "test-key"}.items():
            if isinstance(stack.secrets, MutableSecretsProtocol):
                await stack.secrets.set(ref, val)
            elif isinstance(stack.secrets, DictSecrets):
                stack.secrets._mapping[ref] = val  # type: ignore[attr-defined]
            else:
                monkeypatch.setenv(ref, val)
        async with stack.make_broker() as broker2:
            assert await broker2.count() == 1
            assert (await broker2.get("llm1")).config.name == "llm1"
    else:
        with pytest.raises(EmptyRegistryError, match="sync"):
            async with stack.make_broker():
                pass
