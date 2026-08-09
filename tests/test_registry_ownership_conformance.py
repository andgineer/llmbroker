"""One conformance matrix for invariant 22 and the alias contract.

Two axes vary in production; the existing suites cover one of them. The axis nothing
occupies is *how the entry got into the registry* — and every ownership defect found in
review so far has been a cell of that axis, not of the backend axis.

The expectations here are the contract as written, verbatim:

- `invariants.md` 22 — "a sync never removes or overwrites an entry it did not write.
  […] the default is *no*, so anything reaching a registry by any other route is
  protected without doing anything."
- `decisions.md#a-sync-touches-only-what-a-sync-wrote` — "anything reaching a registry
  by any other route — a host's own mirror call, a hand-written row, a backend filled
  before llmbroker ever ran — is protected without doing anything."
- `rules/direct-aliases.md` — "an alias never sits on a pool member, and every registry
  refuses one on read and on write."

A red cell is therefore a place where the code and the contract disagree. Which of the
two is wrong is a decision for the maintainer, and the point of the matrix is that the
decision gets made once for the whole row instead of once per review round.
"""

import contextlib
from dataclasses import dataclass

import pytest

from llmbroker.broker import presets
from llmbroker.broker.broker import AsyncBroker
from llmbroker.models import LLMConfig
from llmbroker.mongodb import Registry as MongoRegistry
from llmbroker.postgres import Registry as PostgresRegistry
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.standalone.registry import parse_lineup
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import InMemoryStore

_PRESET = (
    '[[llms]]\nname = "gemini"\nbase_url = "https://g/v1"\nmodel = "m"\napi_key_ref = "GEMINI"\n'
)


@pytest.fixture
def preset(monkeypatch):
    monkeypatch.setattr(presets, "fetch_preset_text", lambda _name: _PRESET)


# ── Axis 1: which backend stores the entry ───────────────────────────────────


@pytest.fixture(
    params=["sqlite", "postgres", "mongodb"],
    ids=["sqlite", "postgres", "mongodb"],
)
async def ownership_registry(request, tmp_path_factory, pg_pool, mongo_db):
    param = request.param
    if param == "sqlite":
        yield SqliteRegistry(str(tmp_path_factory.mktemp("own_sqlite") / "reg.db"))
    elif param == "postgres":
        yield PostgresRegistry(pg_pool)
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM llmbroker_registry")
    elif param == "mongodb":
        yield MongoRegistry(mongo_db)
        await mongo_db["llmbroker_registry"].delete_many({})


# ── Axis 2: how the entry got there ──────────────────────────────────────────


@dataclass(frozen=True)
class Provenance:
    """One way a row reaches a registry, and whether the contract calls it the
    installation's own."""

    id: str
    metadata: dict | None
    owned: bool


_HOST_MIRRORED = LLMConfig(name="_", base_url="_", model="_", api_key_ref="_").to_metadata()

_PROVENANCE = [
    # The control: a sync wrote it, so a sync may retire and replace it.
    Provenance("sync", {"synced": True}, owned=False),
    # The documented gesture — usage.md#own-entry.
    Provenance("mirror-llmconfig", _HOST_MIRRORED, owned=True),
    # Written straight into the table, past the protocol: a backend the installation
    # filled itself, carrying whatever it happened to care about.
    Provenance("raw-marked", {"synced": False}, owned=True),
    Provenance("raw-empty-metadata", {}, owned=True),
    Provenance("raw-no-metadata", None, owned=True),
    Provenance("raw-only-weight", {"weight": 0.5}, owned=True),
    Provenance("raw-only-custom", {"custom": True}, owned=True),
]


@pytest.fixture(params=_PROVENANCE, ids=[p.id for p in _PROVENANCE])
def provenance(request) -> Provenance:
    return request.param


async def _place(registry, prov: Provenance, *, name: str, ref: str, url: str) -> None:
    """Put one entry into the registry the way ``prov`` says it got there. Written past
    ``mirror`` on purpose: the routes under test are the ones that do not go through it."""
    await registry._driver.upsert(
        "registry",
        (name,),
        {
            "name": name,
            "base_url": url,
            "model": "m",
            "api_key_ref": ref,
            "metadata": prov.metadata,
        },
    )


def _broker(registry) -> AsyncBroker:
    return AsyncBroker(
        registry=registry,
        secrets=DictSecrets({"GEMINI": "sk", "OWN": "sk"}),
        store=InMemoryStore(),
        sync=None,
    )


# ── Property 1: a sync removes only what a sync wrote ────────────────────────


async def test_a_sync_removes_only_what_a_sync_wrote(ownership_registry, provenance, preset):
    """The entry shares the arriving lineup's key ref, which is the removal rule's
    first row — the strongest removal path there is, and it must stop at the border."""
    registry = ownership_registry
    await _place(registry, provenance, name="mine", ref="GEMINI", url="https://own/v1")
    broker = _broker(registry)
    try:
        await broker.sync("freetier")
    finally:
        await broker.aclose()
    survived = "mine" in {c.name for c in await registry.load()}
    assert survived is provenance.owned


# ── Property 2: a sync never silently rewrites what it did not write ─────────


async def test_a_sync_never_silently_rewrites_what_it_did_not_write(
    ownership_registry, provenance, preset
):
    """A name the arriving lineup also carries. Refusing is a correct outcome; quietly
    replacing the installation's provider fields is not."""
    registry = ownership_registry
    await _place(registry, provenance, name="gemini", ref="GEMINI", url="https://own/v1")
    broker = _broker(registry)
    try:
        with contextlib.suppress(ValueError):
            await broker.sync("freetier")
    finally:
        await broker.aclose()
    stored = {c.name: c for c in await registry.load()}
    expected = "https://g/v1" if not provenance.owned else "https://own/v1"
    assert stored["gemini"].base_url == expected


# ── Property 3: an alias never sits on a pool member, on every route ─────────


def _pooled_alias() -> LLMConfig:
    return LLMConfig(
        name="mine",
        base_url="https://own/v1",
        model="m",
        api_key_ref="OWN",
        alias="opus",
    )


def test_alias_route_file_parse():
    with pytest.raises(ValueError, match="alias"):
        parse_lineup(
            {
                "llms": [
                    {
                        "name": "mine",
                        "base_url": "https://own/v1",
                        "model": "m",
                        "api_key_ref": "OWN",
                        "alias": "opus",
                    },
                ],
            },
        )


async def test_alias_route_driver_mirror(ownership_registry):
    with pytest.raises(ValueError, match="alias"):
        await ownership_registry.mirror([_pooled_alias()])


async def test_alias_route_driver_load(ownership_registry):
    cfg = _pooled_alias()
    await ownership_registry._driver.upsert(
        "registry",
        (cfg.name,),
        {
            "name": cfg.name,
            "base_url": cfg.base_url,
            "model": cfg.model,
            "api_key_ref": cfg.api_key_ref,
            "metadata": {"synced": False, "alias": "opus"},
        },
    )
    with pytest.raises(ValueError, match="alias"):
        await ownership_registry.load()


class HostRegistry:
    """A registry the host implemented itself — the route
    ``decisions.md#a-sync-touches-only-what-a-sync-wrote`` blesses by name."""

    def __init__(self, configs: list[LLMConfig]) -> None:
        self._configs = list(configs)

    async def load(self) -> list[LLMConfig]:
        return list(self._configs)

    async def mirror(self, configs: list[LLMConfig]) -> None:
        self._configs = list(configs)


async def test_alias_route_host_registry_object():
    """Nothing between a host's own ``load`` and the pool re-checks the contract, so
    the guard has to live above the registry, not inside one implementation of it."""
    broker = _broker(HostRegistry([_pooled_alias()]))
    try:
        with pytest.raises(ValueError, match="alias"):
            await broker.ensure_pool()
    finally:
        await broker.aclose()
