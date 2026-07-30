"""Per-user scoping e2e (mission #4): a dead key dropped by one scoped broker
must not affect a sibling broker over the same backend, and each broker's
journal view (``calls()``) stays scoped to its own rows.
"""

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest

from llmbroker.broker.broker import AsyncBroker
from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.models import LLMConfig
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.sqlite import Store as SqliteStore
from llmbroker.standalone.secrets import DictSecrets

_PATCH = "llmbroker.broker.router.call_provider"


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.text = f"HTTP {status}"
    return httpx.HTTPStatusError("err", request=MagicMock(), response=resp)


async def _fake_call_provider(config, api_key, messages, tools, *, client=None, timeout=None):  # noqa: ARG001
    if api_key == "dead-key":
        raise _http_status_error(401)
    return "ok", None, None


async def _seed(db: str) -> None:
    await SqliteRegistry(db).mirror(
        [LLMConfig(name="p1", base_url="https://x/v1", model="m", api_key_ref="KEY")],
    )


def test_scoped_dead_key_does_not_affect_unscoped_sibling(tmp_path, monkeypatch):
    async def run():
        db = str(tmp_path / "b.db")
        await _seed(db)
        secrets = DictSecrets({"KEY": "good-key", "u1/KEY": "dead-key"})
        monkeypatch.setattr("llmbroker.broker.router.call_provider", _fake_call_provider)

        async with AsyncBroker(db, secrets=secrets, scope="u1") as u1_broker:
            with pytest.raises(NoLLMAvailableError):
                await u1_broker.chat([{"role": "user", "content": "hi"}], wait=0)

        async with AsyncBroker(db, secrets=secrets) as unscoped_broker:
            result = await unscoped_broker.chat([{"role": "user", "content": "hi"}])
            assert result.text == "ok"

    asyncio.run(run())


def test_scoped_broker_calls_excludes_other_scopes(tmp_path, monkeypatch):
    """A scoped broker's calls() never leaks another scope's rows; an admin panel
    reading the store backend directly (QueryableStoreProtocol) sees everything —
    that is the documented supported path for a cross-scope view, not an unscoped
    broker."""

    async def run():
        db = str(tmp_path / "b.db")
        await _seed(db)
        secrets = DictSecrets({"KEY": "good-key", "u1/KEY": "good-key"})
        monkeypatch.setattr("llmbroker.broker.router.call_provider", _fake_call_provider)

        async with AsyncBroker(db, secrets=secrets, scope="u1") as u1_broker:
            await u1_broker.chat([{"role": "user", "content": "hi"}])

        async with AsyncBroker(db, secrets=secrets) as unscoped_broker:
            await unscoped_broker.chat([{"role": "user", "content": "hi"}])

        async with AsyncBroker(db, secrets=secrets, scope="u1") as u1_broker:
            u1_rows = await u1_broker.calls(limit=10)

        admin_rows = await SqliteStore(db).calls(limit=10)

        assert u1_rows and all(c.scope == "u1" for c in u1_rows)
        assert {c.scope for c in admin_rows} == {"u1", None}

    asyncio.run(run())
