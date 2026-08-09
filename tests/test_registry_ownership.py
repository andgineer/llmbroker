"""Who owns a registry entry: a sync touches only what a sync wrote.

An entry the installation put in its own registry is carried through every merge —
never removed, never overwritten — and the pool routes it if it is a pool member.
"""

import tomllib
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from llmbroker.broker import presets
from llmbroker.broker.broker import AsyncBroker
from llmbroker.broker.catalog import Catalog
from llmbroker.broker.keys import KeyEvidence
from llmbroker.broker.lineup_file import render_lineup
from llmbroker.broker.merge import merge_upstream, retirement_candidates
from llmbroker.models import Lineup, LLMConfig
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.standalone.registry import parse_lineup
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import InMemoryStore

_ROUTER_CALL = "llmbroker.broker.router.call_provider"

_PRESET = (
    '[[llms]]\nname = "gemini"\nbase_url = "https://g/v1"\nmodel = "m"\napi_key_ref = "GEMINI"\n'
)


@pytest.fixture
def preset(monkeypatch):
    monkeypatch.setattr(presets, "fetch_preset_text", lambda _name: _PRESET)


def _own(name, ref="OWN", *, url="https://own/v1"):
    """An entry the installation stated itself: nothing marks it as from_preset."""
    return LLMConfig(name=name, base_url=url, model="m", api_key_ref=ref)


def _curated(name, ref="GEMINI", *, url="https://g/v1", model="m"):
    return LLMConfig(name=name, base_url=url, model=model, api_key_ref=ref, from_preset=True)


def _merge(new, current, present=()):
    merged, report = merge_upstream(
        Lineup(configs=list(new)),
        Lineup(configs=list(current)),
        KeyEvidence(present=frozenset(present), visible=True),
        source="freetier",
    )
    return merged.configs, report


def _broker(registry, **kwargs):
    kwargs.setdefault("secrets", DictSecrets({"GEMINI": "sk", "OWN": "sk"}))
    kwargs.setdefault("store", InMemoryStore())
    kwargs.setdefault("sync", None)
    return AsyncBroker(registry=registry, **kwargs)


# ── The merge partitions on who wrote the entry ──────────────────────────────


def test_a_host_entry_survives_a_sync_that_adds_updates_and_removes():
    mine = _own("mine")
    merged, report = _merge(
        [_curated("gemini"), _curated("groq", "GROQ", url="https://q2/v1")],
        [mine, _curated("groq", "GROQ", url="https://q/v1"), _curated("gone", "GONE")],
    )
    assert mine in merged
    assert (report.added, report.updated, report.removed) == (("gemini",), ("groq",), ("gone",))
    assert report.kept == ()


def test_an_arriving_entry_never_replaces_a_host_entry_on_the_same_provider():
    """The removal rule's first row is about the provider, and it may not reach here."""
    mine = _own("mine", "GEMINI")
    merged, report = _merge([_curated("gemini")], [mine])
    assert [c.name for c in merged] == ["gemini", "mine"]
    assert report.removed == ()


def test_a_host_entry_is_never_a_retirement_candidate():
    """Retirement is the one destructive act a sync has, and it stops at the border."""
    assert (
        retirement_candidates(
            [_curated("gemini")],
            [_own("mine", "OWN")],
            KeyEvidence(present=frozenset({"OWN"}), visible=True),
        )
        == []
    )


def test_a_name_the_merge_would_carry_twice_is_refused():
    with pytest.raises(ValueError, match="two entries named 'gemini'") as exc:
        _merge([_curated("gemini")], [_own("gemini")])
    assert "rename the entry this installation stated itself" in str(exc.value)


async def test_a_clashing_name_leaves_the_registry_untouched(tmp_path, preset):
    registry = SqliteRegistry(str(tmp_path / "b.db"))
    await registry.mirror([_own("gemini")])
    broker = _broker(registry)
    try:
        with pytest.raises(ValueError, match="two entries named"):
            await broker.sync("freetier")
    finally:
        await broker.aclose()
    assert await registry.load() == [_own("gemini")]


# ── The pool routes what the registry states, whoever wrote it ───────────────


async def test_a_host_entry_is_routed_and_failed_over_to(tmp_path, preset):
    registry = SqliteRegistry(str(tmp_path / "b.db"))
    await registry.mirror([_own("mine")])
    broker = _broker(registry)

    async def _only_mine_answers(config, *_args, **_kwargs):
        if config.name != "mine":
            raise httpx.ConnectError("refused")
        return "ok", None, None

    try:
        await broker.sync("freetier")
        assert set(await broker.snapshot()) == {"gemini", "mine"}
        with patch(_ROUTER_CALL, new=AsyncMock(side_effect=_only_mine_answers)):
            result = await broker.chat([{"role": "user", "content": "hi"}])
        assert (result.text, result.llm_name) == ("ok", "mine")
    finally:
        await broker.aclose()


# ── The new fact is persisted, and so joins the identity gate ────────────────


def test_who_wrote_an_entry_is_part_of_its_identity():
    assert _curated("gemini") != _own("gemini", "GEMINI", url="https://g/v1")


async def test_the_fact_survives_the_registry_round_trip(tmp_path, preset):
    """The identity gate compares stored against merged, so a fact the store drops
    would make every sync a no-op — or every no-op a write."""
    registry = SqliteRegistry(str(tmp_path / "b.db"))
    await registry.mirror([_curated("gemini"), _own("mine")])
    assert {c.name: c.from_preset for c in await registry.load()} == {"gemini": True, "mine": False}


async def test_a_sync_marks_what_it_wrote(tmp_path, preset):
    registry = SqliteRegistry(str(tmp_path / "b.db"))
    broker = _broker(registry)
    try:
        await broker.sync("freetier")
    finally:
        await broker.aclose()
    assert [(c.name, c.from_preset) for c in await registry.load()] == [("gemini", True)]


# ── The lineup file has one owner ───────────────────────────────────────────


def test_a_render_and_parse_round_trip_states_the_file_as_a_syncs():
    """The file is llmbroker's own output, so everything in it came from a preset — a
    renderer that stopped agreeing with the parser would invent a second owner."""
    lineup = Lineup(configs=[_curated("gemini"), _curated("groq")])
    reparsed = parse_lineup(tomllib.loads(render_lineup(lineup)))
    assert {c.name: c.from_preset for c in reparsed.configs} == {"gemini": True, "groq": True}


async def test_a_repeated_sync_over_a_host_entry_applies_nothing(tmp_path, preset):
    registry = SqliteRegistry(str(tmp_path / "b.db"))
    await registry.mirror([_own("mine")])
    broker = _broker(registry)
    try:
        await broker.sync("freetier")
        with patch.object(Catalog, "apply", new=AsyncMock()) as apply:
            await broker.sync("freetier")
        assert apply.await_count == 0
    finally:
        await broker.aclose()
