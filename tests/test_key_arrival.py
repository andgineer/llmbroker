"""A key reaching the pool without the caller noticing, over every secrets backend.

Two shapes, and the caller must not tell them apart: the key was in the store when
the pool was last rebuilt but was never read into memory, and the key was stored
after that rebuild. Both are one provider rate-limiting while a sibling waits behind
a key nobody has resolved yet.
"""

from unittest.mock import MagicMock

import httpx
import pytest

from llmbroker.broker.broker import AsyncBroker
from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.standalone.registry import Registry as FileRegistry
from llmbroker.standalone.store import InMemoryStore

_PATCH = "llmbroker.broker.router.call_provider"
_ENTRY = '[[llms]]\nname="{name}"\nbase_url="https://{name}/v1"\nmodel="m"\napi_key_ref="{ref}"\n'


def _rate_limited() -> httpx.HTTPStatusError:
    response = MagicMock()
    response.status_code = 429
    response.headers = {}
    response.text = "rate limited"
    return httpx.HTTPStatusError("429", request=MagicMock(), response=response)


async def _first_provider_is_rate_limited(
    config, api_key, messages, tools, *, client=None, timeout=None, params=None
):  # noqa: ARG001
    """p1 always refuses; p2 answers and says which key paid for it."""
    if config.name == "p1":
        raise _rate_limited()
    return f"p2 answered with {api_key}", None, None


@pytest.fixture
def registry(tmp_path):
    """The registry backend is immaterial here — the file one needs no driver."""
    target = tmp_path / "llms.toml"
    target.write_text(_ENTRY.format(name="p1", ref="K1") + _ENTRY.format(name="p2", ref="K2"))
    return FileRegistry(target)


def _broker(registry, secrets) -> AsyncBroker:
    return AsyncBroker(
        registry=registry,
        secrets=secrets,
        store=InMemoryStore(),
        sync=None,
        sync_interval=None,
    )


async def test_a_key_held_but_never_read_answers_the_same_call(
    registry,
    mutable_secrets,
    monkeypatch,
):
    """Both keys are in the store before the broker starts, but only p1's is ever
    resolved — until p1 cools down and the failover loop asks for p2's."""
    monkeypatch.setattr(_PATCH, _first_provider_is_rate_limited)
    await mutable_secrets.set("K1", "k1-value")
    await mutable_secrets.set("K2", "k2-value")

    async with _broker(registry, mutable_secrets) as broker:
        result = await broker.ask("hi", wait=0)

    assert result.text == "p2 answered with k2-value"


async def test_a_key_stored_after_the_last_rebuild_answers_the_same_call(
    registry,
    mutable_secrets,
    monkeypatch,
):
    """The admin stores p2's key into a running installation. The next call that
    cannot be served re-reads the store inside the request and answers from it —
    no restart, no waiting out the refresh clock, no error reaching the caller."""
    monkeypatch.setattr(_PATCH, _first_provider_is_rate_limited)
    await mutable_secrets.set("K1", "k1-value")

    async with _broker(registry, mutable_secrets) as broker:
        await broker.ensure_pool()  # the listing taken here does not name K2
        await mutable_secrets.set("K2", "stored-later")

        result = await broker.ask("hi", wait=0)

    assert result.text == "p2 answered with stored-later"


async def test_a_key_that_is_not_there_still_fails(registry, mutable_secrets, monkeypatch):
    """The other half of the contract: the re-read is not a retry loop. Nothing was
    stored, so the caller gets its error rather than a second wait."""
    monkeypatch.setattr(_PATCH, _first_provider_is_rate_limited)
    await mutable_secrets.set("K1", "k1-value")

    async with _broker(registry, mutable_secrets) as broker:
        with pytest.raises(NoLLMAvailableError):
            await broker.ask("hi", wait=0)


async def test_the_scoped_caller_gets_its_own_late_key(registry, mutable_secrets, monkeypatch):
    """The same, for a user who has just stored a key of their own: the ref carries
    their scope, and the re-read is what makes it payable."""
    monkeypatch.setattr(_PATCH, _first_provider_is_rate_limited)
    await mutable_secrets.set("K1", "k1-value")

    async with _broker(registry, mutable_secrets) as broker:
        await broker.ensure_pool()
        await mutable_secrets.set("alice/K2", "alice-own")

        result = await broker.for_scope("alice").ask("hi", wait=0)

    assert result.text == "p2 answered with alice-own"
