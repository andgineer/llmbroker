"""The curated files read as data: no broker, no network, this machine's copy first."""

import pytest

from llmbroker import home
from llmbroker.broker import presets
from llmbroker.broker.curated import (
    CuratedProvider,
    curated_paid,
    curated_pool,
    curated_providers,
    models_from,
    providers_from,
)

_CATALOG = {
    "provider": [
        {
            "id": "anthropic",
            "label": "Anthropic (Claude)",
            "base_url": "https://api.anthropic.com/v1",
            "api_key_ref": "ANTHROPIC_API_KEY",
            "key_help": "console.anthropic.com",
            "models": [
                {"alias": "opus", "model": "claude-opus-5", "label": "Opus 5"},
                {"model": "claude-experimental-1"},
                {"alias": "nothing", "label": "a row with no model id"},
            ],
        },
        {
            "id": "half",
            "api_key_ref": "HALF_KEY",
            "models": [{"alias": "half", "model": "half-1"}],
        },
        {"id": "empty", "base_url": "https://empty/v1", "api_key_ref": "E"},
        "not a table",
    ],
}

_MOUNTED_CATALOG = (
    '[[provider]]\nid="mounted"\nbase_url="https://mounted/v1"\napi_key_ref="MOUNTED_KEY"\n'
    '  [[provider.models]]\n  model="mounted-1"\n'
)

_CACHED_CATALOG = (
    '[[provider]]\nid="cached"\nbase_url="https://cached/v1"\napi_key_ref="CACHED_KEY"\n'
    '  [[provider.models]]\n  alias="cached"\n  model="cached-1"\n'
)


@pytest.fixture(autouse=True)
def never_fetches(monkeypatch):
    """A reader that fetched would put a network call behind an enumeration."""

    def _boom(_name):
        raise AssertionError("the curated readers must never fetch")

    monkeypatch.setattr(presets, "fetch_preset_text", _boom)


def test_models_from_keeps_an_alias_less_row_and_skips_one_with_no_model_id():
    rows = models_from(_CATALOG)
    assert [(row.provider.id, row.model, row.alias) for row in rows] == [
        ("anthropic", "claude-opus-5", "opus"),
        ("anthropic", "claude-experimental-1", None),
        ("half", "half-1", "half"),
    ]


def test_models_from_yields_rows_of_a_provider_missing_a_field():
    (row,) = [r for r in models_from(_CATALOG) if r.provider.id == "half"]
    assert row.provider.base_url == ""
    assert row.provider.api_key_ref == "HALF_KEY"


def test_providers_from_returns_a_provider_carrying_no_models():
    providers = providers_from(_CATALOG)
    assert [p.id for p in providers] == ["anthropic", "half", "empty"]
    assert providers[0].label == "Anthropic (Claude)"
    assert providers[0].key_help == "console.anthropic.com"


def test_a_provider_declares_a_model_the_catalog_does_not_carry():
    provider = providers_from(_CATALOG)[0]
    cfg = provider.declare("claude-unreleased-9")
    assert cfg.name == "anthropic-claude-unreleased-9"
    assert cfg.model == "claude-unreleased-9"
    assert cfg.base_url == "https://api.anthropic.com/v1"
    assert cfg.api_key_ref == "ANTHROPIC_API_KEY"
    assert cfg.alias is None
    assert cfg.from_preset is False


def test_a_curated_row_declares_itself_with_its_alias():
    row = models_from(_CATALOG)[0]
    cfg = row.declare()
    assert (cfg.name, cfg.model, cfg.alias) == ("anthropic-claude-opus-5", "claude-opus-5", "opus")
    assert models_from(_CATALOG)[1].declare().alias is None


def test_the_readers_fall_to_the_wheels_copy_when_nothing_is_cached(
    llmbroker_home,
    bundled_presets,
):
    paid = curated_paid()
    assert [row.alias for row in paid if row.alias][:1] == ["opus"]
    assert all(row.provider.base_url.startswith("https://") for row in paid)
    assert {p.id for p in curated_providers()} == {row.provider.id for row in paid}
    assert curated_pool().configs


def test_a_cached_copy_wins_over_the_wheels(llmbroker_home, bundled_presets):
    cache = llmbroker_home / "presets"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "paid-catalog.toml").write_text(_CACHED_CATALOG, encoding="utf-8")
    assert [(row.provider.id, row.model) for row in curated_paid()] == [("cached", "cached-1")]


def test_home_points_the_read_at_another_machine_copy(tmp_path, bundled_presets):
    cache = tmp_path / "presets"
    cache.mkdir(parents=True)
    (cache / "paid-catalog.toml").write_text(_CACHED_CATALOG, encoding="utf-8")
    assert [row.model for row in curated_paid(home=tmp_path)] == ["cached-1"]
    assert [p.id for p in curated_providers(home=tmp_path)] == ["cached"]


def test_home_is_read_where_it_cannot_be_written(tmp_path, monkeypatch, llmbroker_home):
    """A catalog on a read-only mount is a perfectly good read: falling through to
    another machine directory would answer with a different provider's endpoints."""
    monkeypatch.setattr(home, "_is_writable", lambda path: path != tmp_path)
    cache = llmbroker_home / "presets"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "paid-catalog.toml").write_text(_CACHED_CATALOG, encoding="utf-8")
    (tmp_path / "presets").mkdir()
    (tmp_path / "presets" / "paid-catalog.toml").write_text(_MOUNTED_CATALOG, encoding="utf-8")
    assert [row.model for row in curated_paid(home=tmp_path)] == ["mounted-1"]
    assert [p.id for p in curated_providers(home=tmp_path)] == ["mounted"]


def test_a_read_creates_nothing(tmp_path, bundled_presets):
    absent = tmp_path / "absent"
    assert curated_paid(home=absent)
    assert not absent.exists()


def test_a_declared_provider_is_a_plain_dataclass():
    provider = CuratedProvider(id="p", base_url="https://p/v1", api_key_ref="P")
    assert provider.declare("m").name == "p-m"
    assert provider.key_help == ""
