"""The identity gate: a sync whose merged result equals what is already there
writes nothing, applies nothing and stays out of the INFO log."""

import logging
from unittest.mock import AsyncMock, patch

from llmbroker.broker import presets
from llmbroker.broker.broker import AsyncBroker
from llmbroker.broker.catalog import Catalog
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.sqlite import Secrets as SqliteSecrets
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import InMemoryStore

_NEW = (
    '[[llms]]\nname = "gemini"\nbase_url = "https://g/v1"\nmodel = "m"\n'
    'api_key_ref = "GEMINI_API_KEY"\n\n[keys.GEMINI_API_KEY]\nhelp = "gemini help"\n'
)

# Deliberately not in name order: a database registry hands rows back sorted by
# name, so this is what tells a real change from the backend's own ordering.
_UNSORTED = (
    '[[llms]]\nname = "zeta"\nbase_url = "https://z/v1"\nmodel = "m"\napi_key_ref = "Z_KEY"\n'
    '[[llms]]\nname = "alpha"\nbase_url = "https://a/v1"\nmodel = "m"\napi_key_ref = "A_KEY"\n'
)


# ── The file target ──────────────────────────────────────────────────────────
#
# The byte-level gate on the rendered file is in test_lineup_file.py; here it is
# the broker's own outcome that matters.


async def test_a_file_broker_reports_no_change_at_debug(tmp_path, caplog, monkeypatch):
    """The first sync of a hand-written file rewrites it into the form llmbroker
    renders; from there on an unchanged preset is a no-op."""
    monkeypatch.setattr(presets, "fetch_preset_text", lambda _name: _NEW)
    target = tmp_path / "llms.toml"
    target.write_text(_NEW)
    broker = AsyncBroker(
        Registry(target),
        secrets=DictSecrets({"GEMINI_API_KEY": "sk"}),
        store=InMemoryStore(),
    )
    try:
        await broker.sync("freetier")
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="llmbroker.broker"):
            await broker.sync("freetier")
    finally:
        await broker.aclose()
    assert any("no change" in r.message for r in caplog.records if r.levelno == logging.DEBUG)
    assert not any(r.levelno == logging.INFO for r in caplog.records)


# ── The registry target ──────────────────────────────────────────────────────


def _preset(tmp_path, body: str, name: str = "preset.toml") -> str:
    src = tmp_path / name
    src.write_text(body)
    return str(src)


def _broker(tmp_path, **kwargs) -> AsyncBroker:
    kwargs.setdefault("secrets", DictSecrets({"Z_KEY": "sk", "A_KEY": "sk"}))
    return AsyncBroker(
        registry=SqliteRegistry(str(tmp_path / "b.db")),
        store=InMemoryStore(),
        **kwargs,
    )


async def test_an_unchanged_registry_sync_writes_nothing(tmp_path, caplog):
    src = _preset(tmp_path, _UNSORTED)
    broker = _broker(tmp_path)
    try:
        report = await broker.sync(src)
        assert report.added == ("zeta", "alpha")
        with (
            patch.object(Catalog, "apply", new=AsyncMock()) as apply,
            caplog.at_level(logging.DEBUG, logger="llmbroker.broker"),
        ):
            await broker.sync(src)
        assert apply.await_count == 0
    finally:
        await broker.aclose()
    assert any("no change" in r.message for r in caplog.records)


async def test_the_gate_ignores_the_order_a_registry_returns_rows_in(tmp_path):
    """The lineup arrives in curated order and comes back name-sorted; comparing
    the two as lists would report every no-op sync as a change."""
    src = _preset(tmp_path, _UNSORTED)
    broker = _broker(tmp_path)
    try:
        await broker.sync(src)
        stored = await broker._registry.load()
        assert [c.name for c in stored] == ["alpha", "zeta"]  # not the lineup's order
        with patch.object(Catalog, "apply", new=AsyncMock()) as apply:
            await broker.sync(src)
        assert apply.await_count == 0
    finally:
        await broker.aclose()


async def test_a_real_change_still_applies_and_logs_at_info(tmp_path, caplog):
    broker = _broker(tmp_path)
    try:
        await broker.sync(_preset(tmp_path, _UNSORTED))
        grown = _UNSORTED + (
            '[[llms]]\nname = "mid"\nbase_url = "https://m/v1"\nmodel = "m"\n'
            'api_key_ref = "M_KEY"\n'
        )
        with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
            report = await broker.sync(_preset(tmp_path, grown, "grown.toml"))
        assert report.added == ("mid",)
        assert [c.name for c in await broker._registry.load()] == ["alpha", "mid", "zeta"]
    finally:
        await broker.aclose()
    assert any(r.levelno == logging.INFO and "sync" in r.message for r in caplog.records)


async def test_a_reweighted_lineup_is_a_change_to_the_registry_target(tmp_path):
    """The registry branch compares entries by name, so a new persisted field joins
    that comparison by itself — a weight-only edit must not read as a no-op."""
    broker = _broker(tmp_path)
    try:
        await broker.sync(_preset(tmp_path, _UNSORTED))
        reweighted = _UNSORTED.replace('api_key_ref = "Z_KEY"', 'api_key_ref = "Z_KEY"\nweight=0.8')
        with patch.object(Catalog, "apply", new=AsyncMock()) as apply:
            await broker.sync(_preset(tmp_path, reweighted, "reweighted.toml"))
        assert apply.await_count == 1
    finally:
        await broker.aclose()


async def test_an_unchanged_sync_still_bootstraps_a_key_that_arrived(tmp_path, monkeypatch):
    """The gate covers the lineup, not the keys: a key exported after the first
    sync is what the next explicit sync is called for."""
    db = str(tmp_path / "b.db")
    monkeypatch.delenv("Z_KEY", raising=False)
    monkeypatch.delenv("A_KEY", raising=False)
    src = _preset(tmp_path, _UNSORTED)

    broker = AsyncBroker(
        registry=SqliteRegistry(db),
        secrets=SqliteSecrets(db),
        store=InMemoryStore(),
    )
    try:
        await broker.sync(src)
        monkeypatch.setenv("Z_KEY", "sk-late")
        await broker.sync(src)
    finally:
        await broker.aclose()

    secrets = SqliteSecrets(db)
    try:
        assert await secrets.resolve("Z_KEY") == "sk-late"
    finally:
        await secrets.aclose()
