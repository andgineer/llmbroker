"""One broker per process, a caller per request: two callers routing on their own
keys over one shared pool, each writing its own scope, and neither paying any I/O
to exist."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from llmbroker.broker.broker import AsyncBroker
from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.models import LLMConfig
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.sqlite import Store as SqliteStore
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import InMemoryStore

_PATCH = "llmbroker.broker.router.call_provider"


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.text = f"HTTP {status}"
    return httpx.HTTPStatusError("err", request=MagicMock(), response=resp)


async def _keys_used(config, api_key, messages, tools, *, client=None, timeout=None):  # noqa: ARG001
    if api_key == "dead-key":
        raise _http_status_error(401)
    return api_key, None, None


def _cfg(name="p1", ref="KEY", parallel=None):
    return LLMConfig(
        name=name,
        base_url="https://x/v1",
        model="m",
        api_key_ref=ref,
        parallel=parallel,
    )


async def _broker(tmp_path, secrets, configs=(_cfg(),), store=None):
    db = str(tmp_path / "b.db")
    await SqliteRegistry(db).mirror(list(configs))
    return AsyncBroker(
        SqliteRegistry(db),
        secrets=DictSecrets(secrets),
        store=store if store is not None else InMemoryStore(),
        sync=None,
    )


# ── Two callers, two keys, one pool ──────────────────────────────────────────


async def test_each_caller_routes_on_its_own_key(tmp_path, monkeypatch):
    monkeypatch.setattr(_PATCH, _keys_used)
    broker = await _broker(
        tmp_path,
        {"KEY": "shared-key", "alice/KEY": "alice-key", "bob/KEY": "bob-key"},
    )
    async with broker:
        assert (await broker.for_scope("alice").ask("hi")).text == "alice-key"
        assert (await broker.for_scope("bob").ask("hi")).text == "bob-key"


async def test_a_caller_without_a_key_of_its_own_pays_with_the_shared_one(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(_PATCH, _keys_used)
    broker = await _broker(tmp_path, {"KEY": "shared-key", "alice/KEY": "alice-key"})
    async with broker:
        assert (await broker.for_scope("carol").ask("hi")).text == "shared-key"
        assert (await broker.llms.ask("hi")).text == "shared-key"


async def test_each_caller_writes_its_own_scope_on_its_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(_PATCH, _keys_used)
    broker = await _broker(
        tmp_path, {"KEY": "shared-key"}, store=SqliteStore(str(tmp_path / "j.db"))
    )
    async with broker:
        await broker.for_scope("alice").ask("hi")
        await broker.for_scope("bob").ask("hi")
        await broker.llms.ask("hi")
        rows = await broker.calls(limit=10, kind="call")
    assert {r.scope for r in rows} == {"alice", "bob", None}


async def test_a_delayed_rating_carries_the_callers_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(_PATCH, _keys_used)
    broker = await _broker(
        tmp_path, {"KEY": "shared-key"}, store=SqliteStore(str(tmp_path / "j.db"))
    )
    async with broker:
        await broker.for_scope("alice").record_quality("p1", "summarize", 1.0)
        rows = await broker.calls(limit=10, kind="quality")
    assert [r.scope for r in rows] == ["alice"]


# ── A caller costs nothing to make ───────────────────────────────────────────


async def test_making_a_caller_performs_no_port_io(tmp_path, monkeypatch):
    """A caller is built per request, so anything it reads at construction is read
    on every request. Counted over the ports, not asserted on one call."""
    monkeypatch.setattr(_PATCH, _keys_used)
    reads: list[str] = []

    class _CountingSecrets:
        def __init__(self, mapping):
            self._mapping = mapping

        async def resolve(self, ref):
            reads.append(ref)
            return self._mapping[ref]

    db = str(tmp_path / "b.db")
    await SqliteRegistry(db).mirror([_cfg()])
    broker = AsyncBroker(
        SqliteRegistry(db),
        secrets=_CountingSecrets({"KEY": "shared-key"}),
        store=InMemoryStore(),
        sync=None,
    )
    async with broker:
        reads.clear()
        callers = [broker.for_scope(f"user-{i}") for i in range(50)]
        assert reads == []
        assert len({id(c) for c in callers}) == 50


async def test_the_same_scope_keeps_the_same_keys(tmp_path, monkeypatch):
    """A caller object is free to build per request; what must not be rebuilt per
    request is the ring behind it, or every request would re-read the key."""
    monkeypatch.setattr(_PATCH, _keys_used)
    reads: list[str] = []

    class _CountingSecrets:
        def __init__(self, mapping):
            self._mapping = mapping

        async def resolve(self, ref):
            reads.append(ref)
            return self._mapping[ref]

    db = str(tmp_path / "b.db")
    await SqliteRegistry(db).mirror([_cfg()])
    broker = AsyncBroker(
        SqliteRegistry(db),
        secrets=_CountingSecrets({"KEY": "shared-key", "alice/KEY": "alice-key"}),
        store=InMemoryStore(),
        sync=None,
    )
    async with broker:
        await broker.for_scope("alice").ask("hi")
        reads.clear()
        for _ in range(5):
            assert (await broker.for_scope("alice").ask("hi")).text == "alice-key"
        assert reads == []


async def test_an_empty_scope_is_refused(tmp_path):
    broker = await _broker(tmp_path, {"KEY": "k"})
    async with broker:
        with pytest.raises(ValueError, match="must not be empty"):
            broker.for_scope("")


# ── One pool, one client, one cap ────────────────────────────────────────────


async def test_parallel_one_holds_across_two_callers(tmp_path):
    """The slot counter is what holds a provider to its cap, so it must be the pool's
    and not the caller's — one counter per user is not a cap."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking(config, api_key, messages, tools, *, client=None, timeout=None):  # noqa: ARG001
        started.set()
        await release.wait()
        return "ok", None, None

    broker = await _broker(tmp_path, {"KEY": "k"}, configs=[_cfg(parallel=1)])
    async with broker:
        with patch(_PATCH, new=_blocking):
            first = asyncio.ensure_future(broker.for_scope("alice").ask("hi"))
            await asyncio.wait_for(started.wait(), timeout=2.0)
            with pytest.raises(NoLLMAvailableError):
                await broker.for_scope("bob").ask("hi", wait=0)
            release.set()
            assert (await first).text == "ok"


async def test_many_callers_share_one_http_client(tmp_path, monkeypatch):
    monkeypatch.setattr(_PATCH, _keys_used)
    broker = await _broker(tmp_path, {"KEY": "k"})
    async with broker:
        clients = set()
        for scope in ("alice", "bob", "carol"):
            await broker.for_scope(scope).ask("hi")
            clients.add(id(broker._router.http))
        assert len(clients) == 1


# ── A dead key belongs to the caller that spent it ───────────────────────────


async def test_a_dead_key_drops_one_callers_resolution_only(tmp_path, monkeypatch):
    monkeypatch.setattr(_PATCH, _keys_used)
    broker = await _broker(
        tmp_path,
        {"KEY": "shared-key", "alice/KEY": "dead-key"},
    )
    async with broker:
        with pytest.raises(NoLLMAvailableError):
            await broker.for_scope("alice").ask("hi", wait=0)

        assert (await broker.for_scope("bob").ask("hi")).text == "shared-key"
        assert (await broker.llms.ask("hi")).text == "shared-key"


async def test_a_dead_shared_key_withdraws_the_model_from_every_shared_caller(
    tmp_path,
    monkeypatch,
):
    """The shared value is one credential: a caller that met a 401 on it says nothing
    about a caller holding a key of its own, and everything about its neighbours."""
    monkeypatch.setattr(_PATCH, _keys_used)
    broker = await _broker(tmp_path, {"KEY": "dead-key", "bob/KEY": "bob-key"})
    async with broker:
        with pytest.raises(NoLLMAvailableError):
            await broker.for_scope("alice").ask("hi", wait=0)

        with pytest.raises(NoLLMAvailableError):
            await broker.llms.ask("hi", wait=0)
        assert (await broker.for_scope("bob").ask("hi")).text == "bob-key"


# ── The broker's own verbs are the unscoped caller ───────────────────────────


async def test_the_broker_verbs_are_the_unscoped_caller(tmp_path, monkeypatch):
    monkeypatch.setattr(_PATCH, AsyncMock(return_value=("ok", None, None)))
    broker = await _broker(tmp_path, {"KEY": "k"})
    async with broker:
        assert broker.llms.scope is None
        assert (await broker.ask("hi")).text == "ok"
        assert await broker.count() == await broker.llms.count()
        assert (await broker.get("p1")).config == (await broker.llms.get("p1")).config


class _CostlySecrets:
    """A store where every lookup is a network round trip — Vault, AWS, a DB."""

    def __init__(self, mapping):
        self._mapping = dict(mapping)
        self.reads: list[str] = []

    async def resolve(self, ref):
        self.reads.append(ref)
        if ref not in self._mapping:
            raise KeyError(ref)
        return self._mapping[ref]

    async def refs(self, prefix=""):
        return frozenset(r for r in self._mapping if r.startswith(prefix))


async def test_first_time_callers_cost_the_secrets_store_nothing(tmp_path, monkeypatch):
    """A per-user deployment meets new scopes constantly. None of them may cost a
    round trip for a ref the last listing already said nobody holds."""
    monkeypatch.setattr(_PATCH, _keys_used)
    secrets = _CostlySecrets({"KEY": "shared-key"})
    db = str(tmp_path / "b.db")
    await SqliteRegistry(db).mirror([_cfg(), _cfg(name="p2", ref="KEY2")])
    broker = AsyncBroker(SqliteRegistry(db), secrets=secrets, store=InMemoryStore(), sync=None)
    async with broker:
        await broker.ensure_pool()
        secrets.reads.clear()

        for i in range(200):
            await broker.for_scope(f"user-{i}").ask("hi")

        assert secrets.reads == []


async def test_a_caller_reads_back_its_own_history(tmp_path, monkeypatch):
    """A host giving each user a key of their own must be able to show that user its
    own calls; the broker's own read stays the whole installation's view."""
    monkeypatch.setattr(_PATCH, _keys_used)
    broker = await _broker(
        tmp_path, {"KEY": "shared-key"}, store=SqliteStore(str(tmp_path / "j.db"))
    )
    async with broker:
        await broker.for_scope("alice").ask("hi")
        await broker.for_scope("bob").ask("hi")
        await broker.llms.ask("hi")

        assert [r.scope for r in await broker.for_scope("alice").calls(limit=10)] == ["alice"]
        assert len(await broker.calls(limit=10, kind="call")) == 3

        alice_total = sum(s.total for s in (await broker.for_scope("alice").stats()).values())
        assert (alice_total, sum(s.total for s in (await broker.stats()).values())) == (1, 3)


async def test_a_callers_journal_read_never_provisions_the_pool(tmp_path):
    """Invariant 6 holds for a caller's read exactly as for the broker's."""
    broker = await _broker(
        tmp_path, {"KEY": "k"}, configs=(), store=SqliteStore(str(tmp_path / "j.db"))
    )
    assert await broker.for_scope("alice").calls(limit=10) == []
    assert broker._provisioned is False
    await broker.aclose()
