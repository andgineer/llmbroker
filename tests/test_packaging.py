"""The curated presets ship inside the package.

Without them the refresh's fallback chain has no floor: a first run with no
network and a cold cache would have nothing to start from.
"""

import tomllib
from importlib import resources

import pytest

from llmbroker.broker.upstream import bundled_preset_text

_SHIPPED = ("freetier", "paid-catalog")


@pytest.mark.parametrize("name", _SHIPPED)
def test_a_shipped_preset_is_importable_package_data(name):
    text = bundled_preset_text(name)
    assert text is not None
    assert tomllib.loads(text)


def test_the_bundled_freetier_preset_carries_the_pool():
    data = tomllib.loads(bundled_preset_text("freetier") or "")
    assert [e["name"] for e in data["llms"]]
    assert all(e["base_url"].startswith("https://") for e in data["llms"])


def test_the_bundled_paid_catalog_carries_aliases():
    data = tomllib.loads(bundled_preset_text("paid-catalog") or "")
    aliases = [m["alias"] for p in data["provider"] for m in p.get("models", []) if m.get("alias")]
    assert aliases


def test_an_unknown_name_has_no_bundled_copy():
    assert bundled_preset_text("no-such-preset") is None


def test_the_refresh_prompts_live_beside_what_they_regenerate():
    """A curator finds the prompt next to the preset it rewrites. The wheel excludes
    them — they are maintainer instructions, not payload."""
    presets = resources.files("llmbroker").joinpath("presets")
    assert presets.joinpath("freetier-refresh-prompt.md").is_file()
