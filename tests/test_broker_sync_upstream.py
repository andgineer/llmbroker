"""broker.sync() against a curated preset: the removal rule, have_keys, scope,
the file write-back, the structural refusal, and the one log line per run.

The fetch is always monkeypatched — no test goes to the network.
"""

import logging
import tomllib

import pytest

from llmbroker.broker import upstream
from llmbroker.broker.broker import AsyncBroker
from llmbroker.exceptions import SyncRefusedError
from llmbroker.models import LLMConfig
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.standalone.registry import Registry as FileRegistry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import InMemoryStore

_OLD = LLMConfig(name="groq-old", base_url="https://groq/v1", model="m", api_key_ref="GROQ")

_PRESET_GEMINI = (
    '[[llms]]\nname = "gemini"\nbase_url = "https://g/v1"\nmodel = "m"\n'
    'api_key_ref = "GEMINI"\n\n[keys.GEMINI]\nhelp = "get one at aistudio"\n'
)
_PRESET_GROQ_NEW = (
    '[[llms]]\nname = "groq-new"\nbase_url = "https://groq/v1"\nmodel = "m"\napi_key_ref = "GROQ"\n'
)


@pytest.fixture
def preset(monkeypatch):
    """Serve a preset body to ``sync("freetier")`` without touching the network."""
    served = {"text": _PRESET_GEMINI}

    def fake_fetch(name: str) -> str:
        assert name == "freetier"
        return served["text"]

    monkeypatch.setattr(upstream, "fetch_preset_text", fake_fetch)
    return served


async def _seeded_db(tmp_path, configs=(_OLD,)):
    db = str(tmp_path / "b.db")
    await SqliteRegistry(db).mirror(list(configs))
    return db


def _broker(db, **kwargs):
    kwargs.setdefault("secrets", DictSecrets({}))
    return AsyncBroker(registry=SqliteRegistry(db), store=InMemoryStore(), **kwargs)


# ── The removal rule, through a real registry ────────────────────────────────


async def test_without_the_arrivals_key_the_old_entry_stays_and_keeps_routing(tmp_path, preset):
    db = await _seeded_db(tmp_path)
    broker = _broker(db, secrets=DictSecrets({"GROQ": "sk-groq"}))
    report = await broker.sync("freetier")
    assert report.kept == ("groq-old",)

    async with broker:
        assert {c.name for c in await SqliteRegistry(db).load()} == {"gemini", "groq-old"}
        assert broker._pool.has_key("groq-old")
        assert not broker._pool.has_key("gemini")


async def test_with_the_arrivals_key_present_the_old_entry_goes(tmp_path, preset):
    db = await _seeded_db(tmp_path)
    broker = _broker(db, secrets=DictSecrets({"GROQ": "sk-groq", "GEMINI": "sk-gem"}))
    report = await broker.sync("freetier")
    await broker.aclose()
    assert (report.removed, report.kept) == (("groq-old",), ())
    assert {c.name for c in await SqliteRegistry(db).load()} == {"gemini"}


async def test_a_same_ref_replacement_needs_no_key(tmp_path, preset):
    preset["text"] = _PRESET_GROQ_NEW
    db = await _seeded_db(tmp_path)
    broker = _broker(db)
    report = await broker.sync("freetier")
    await broker.aclose()
    assert report.removed == ("groq-old",)
    assert {c.name for c in await SqliteRegistry(db).load()} == {"groq-new"}


# ── have_keys ────────────────────────────────────────────────────────────────


async def test_have_keys_pays_for_the_removal_without_any_stored_key(tmp_path, preset):
    db = await _seeded_db(tmp_path)
    broker = _broker(db, have_keys=["GEMINI"])
    report = await broker.sync("freetier")
    await broker.aclose()
    assert report.removed == ("groq-old",)
    assert {c.name for c in await SqliteRegistry(db).load()} == {"gemini"}


async def test_have_keys_never_makes_a_model_routable(tmp_path, preset):
    """The promise buys a removal, not a call: the slot stays keyless."""
    db = await _seeded_db(tmp_path)
    broker = _broker(db, have_keys=True)
    await broker.sync("freetier")
    async with broker:
        assert "gemini" in broker._pool.configs
        assert not broker._pool.has_key("gemini")


async def test_a_scoped_broker_counts_its_own_prefixed_ref_as_present(tmp_path, preset):
    db = await _seeded_db(tmp_path)
    broker = _broker(db, scope="alice", secrets=DictSecrets({"alice/GEMINI": "sk-alice"}))
    report = await broker.sync("freetier")
    await broker.aclose()
    assert report.removed == ("groq-old",)


async def test_an_unscoped_probe_that_finds_nothing_removes_nothing(tmp_path, preset):
    """Per-user installations resolve no shared key by design — and so prune nothing."""
    db = await _seeded_db(tmp_path)
    broker = _broker(db, scope="alice")
    report = await broker.sync("freetier")
    await broker.aclose()
    assert report.kept == ("groq-old",)


# ── The file target ──────────────────────────────────────────────────────────


async def test_a_file_registry_is_rewritten_keeping_custom_entries_and_comments(tmp_path, preset):
    target = tmp_path / "llms.toml"
    target.write_text(
        "# my own header\n"
        '[[llms]]\nname="groq-old"\nbase_url="https://groq/v1"\nmodel="m"\napi_key_ref="GROQ"\n'
        '[[custom]]\nname="mine"\nbase_url="https://mine/v1"\nmodel="big"\napi_key_ref="MY_KEY"'
        "\npool=false\n"
        '[keys.MY_KEY]\nhelp = "my custom key"\n',
    )
    broker = AsyncBroker(registry=FileRegistry(target), store=InMemoryStore())
    report = await broker.sync("freetier")
    await broker.aclose()

    data = tomllib.loads(target.read_text())
    assert [e["name"] for e in data["llms"]] == ["gemini", "groq-old"]
    assert [e["name"] for e in data["custom"]] == ["mine"]
    assert data["custom"][0]["pool"] is False
    assert data["keys"]["MY_KEY"]["help"] == "my custom key"
    assert data["keys"]["GEMINI"]["help"] == "get one at aistudio"
    assert report.kept == ("groq-old",)


async def test_a_file_syncs_alias_entry_follows_the_paid_catalog(tmp_path, monkeypatch):
    """The broker's file write-back keeps the CLI's alias refresh — one network seam
    serves both the preset and the catalog."""
    catalog = (
        '[[provider]]\nid="anthropic"\nbase_url="https://api.anthropic.com/v2"\n'
        'api_key_ref="ANTHROPIC_API_KEY"\nkey_help="console.anthropic.com"\n'
        '  [[provider.models]]\n  alias="opus"\n  model="claude-opus-5"\n'
    )
    monkeypatch.setattr(
        upstream,
        "fetch_preset_text",
        lambda name: catalog if name == "paid-catalog" else _PRESET_GEMINI,
    )
    target = tmp_path / "llms.toml"
    target.write_text(
        '[[custom]]\nalias="opus"\nname="anthropic-claude-opus-4-8"\nmodel="claude-opus-4-8"'
        '\nbase_url="https://api.anthropic.com/v1"\napi_key_ref="ANTHROPIC_API_KEY"\npool=false\n',
    )
    broker = AsyncBroker(registry=FileRegistry(target), store=InMemoryStore())
    await broker.sync("freetier")
    await broker.aclose()
    (entry,) = tomllib.loads(target.read_text())["custom"]
    assert (entry["model"], entry["name"]) == ("claude-opus-5", "anthropic-claude-opus-5")


async def test_a_kept_entry_does_not_duplicate_over_repeated_file_syncs(tmp_path, preset):
    target = tmp_path / "llms.toml"
    target.write_text(
        '[[llms]]\nname="groq-old"\nbase_url="https://groq/v1"\nmodel="m"\napi_key_ref="GROQ"\n',
    )
    broker = AsyncBroker(registry=FileRegistry(target), store=InMemoryStore())
    for _ in range(3):
        report = await broker.sync("freetier")
    await broker.aclose()
    assert [e["name"] for e in tomllib.loads(target.read_text())["llms"]] == ["gemini", "groq-old"]
    assert report.kept == ("groq-old",)
    assert report.added == ()


# ── The structural refusal ───────────────────────────────────────────────────


async def test_the_refusal_leaves_the_registry_untouched_and_carries_the_report(tmp_path):
    """Hand-built merge inputs are the only way to reach the guard, so it is checked
    where it lives — the rule itself can never produce an empty lineup."""
    db = await _seeded_db(tmp_path)
    current = await SqliteRegistry(db).load()
    _merged, _keys, report = upstream.merge_upstream([], {}, current, {}, set(), source="freetier")
    with pytest.raises(SyncRefusedError) as excinfo:
        upstream.check_not_emptying([], current, report)
    assert excinfo.value.report.applied is False
    assert {c.name for c in await SqliteRegistry(db).load()} == {"groq-old"}


# ── Source dispatch and the returned report ──────────────────────────────────


async def test_a_path_source_needs_no_network(tmp_path, monkeypatch):
    def explode(_name):
        raise AssertionError("a path source must never fetch")

    monkeypatch.setattr(upstream, "fetch_preset_text", explode)
    src = tmp_path / "lineup.toml"
    src.write_text(
        '[[llms]]\nname="gemini"\nbase_url="https://g/v1"\nmodel="m"\napi_key_ref="GEMINI"\n',
    )
    db = await _seeded_db(tmp_path)
    broker = _broker(db)
    report = await broker.sync(str(src))
    await broker.aclose()
    assert report.source == str(src)
    assert report.added == ("gemini",)


async def test_an_unusable_source_string_names_both_accepted_forms(tmp_path):
    broker = _broker(await _seeded_db(tmp_path))
    with pytest.raises(ValueError, match="preset name"):
        await broker.sync("not a preset/nor a path.toml")
    await broker.aclose()


async def test_a_fetch_failure_raises_from_the_explicit_call(tmp_path, monkeypatch):
    def fail(_name):
        raise ValueError("preset 'freetier' not found in catalog")

    monkeypatch.setattr(upstream, "fetch_preset_text", fail)
    db = await _seeded_db(tmp_path)
    broker = _broker(db)
    with pytest.raises(ValueError, match="not found in catalog"):
        await broker.sync("freetier")
    await broker.aclose()
    assert {c.name for c in await SqliteRegistry(db).load()} == {"groq-old"}


# ── One log line per sync ────────────────────────────────────────────────────


async def _sync_and_capture(broker, caplog, source="freetier"):
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
        await broker.sync(source)
    return [r for r in caplog.records if r.name == "llmbroker.broker"]


async def test_a_run_that_keeps_an_entry_logs_exactly_one_warning(tmp_path, preset, caplog):
    broker = _broker(await _seeded_db(tmp_path))
    records = await _sync_and_capture(broker, caplog)
    await broker.aclose()
    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "kept: groq-old" in warnings[0].message


async def test_a_clean_change_logs_exactly_one_info(tmp_path, preset, caplog):
    preset["text"] = _PRESET_GROQ_NEW
    broker = _broker(await _seeded_db(tmp_path), secrets=DictSecrets({"GROQ": "sk"}))
    records = await _sync_and_capture(broker, caplog)
    await broker.aclose()
    assert [r.levelno for r in records] == [logging.INFO]
    assert "removed: groq-old" in records[0].message


async def test_a_pending_key_alone_is_not_a_warning(tmp_path, preset, caplog):
    """A keyless entry is a normal documented state — and under per-user secrets
    every shared probe fails by design."""
    preset["text"] = _PRESET_GROQ_NEW
    broker = _broker(await _seeded_db(tmp_path))
    records = await _sync_and_capture(broker, caplog)
    await broker.aclose()
    assert [r.levelno for r in records] == [logging.INFO]
    assert "pending key GROQ" in records[0].message


async def test_a_no_op_run_still_logs_its_one_line(tmp_path, preset, caplog):
    preset["text"] = _PRESET_GROQ_NEW
    db = await _seeded_db(
        tmp_path,
        [LLMConfig(name="groq-new", base_url="https://groq/v1", model="m", api_key_ref="GROQ")],
    )
    broker = _broker(db, secrets=DictSecrets({"GROQ": "sk"}))
    records = await _sync_and_capture(broker, caplog)
    await broker.aclose()
    assert [r.levelno for r in records] == [logging.INFO]
    assert records[0].message.endswith("no changes")
