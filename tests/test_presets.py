"""The one network seam: fetching a curated text, and the precedence over the
copies already on the machine."""

import http.client
import urllib.error

import pytest

from llmbroker.broker import presets
from llmbroker.broker.presets import PresetSource, fetch_preset_text

# ── Preset name parsing ──────────────────────────────────────────────────────


def test_fetch_preset_text_refuses_an_invalid_name():
    with pytest.raises(ValueError, match="invalid preset name"):
        fetch_preset_text("../etc/passwd")


# ── Fetch failures are all ValueError, connect phase or not ──────────────────


class _Response:
    """A urlopen context manager whose body dies mid-read."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        raise self._exc


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("timed out"),
        ConnectionResetError("peer went away"),
        http.client.IncompleteRead(b"partial"),
    ],
)
def test_a_body_that_dies_mid_read_is_a_value_error(monkeypatch, exc):
    """The docstring promises every failure is a ValueError; `sync=` and the CLI
    both catch on that promise, and only the connect phase used to keep it."""
    monkeypatch.setattr(presets.urllib.request, "urlopen", lambda *a, **k: _Response(exc))
    with pytest.raises(ValueError, match="failed reading"):
        fetch_preset_text("freetier")


def test_an_undecodable_body_is_a_value_error(monkeypatch):
    class _Bytes(_Response):
        def read(self):
            return b"\xff\xfe not utf-8"

    monkeypatch.setattr(presets.urllib.request, "urlopen", lambda *a, **k: _Bytes(None))
    with pytest.raises(ValueError, match="not valid UTF-8"):
        fetch_preset_text("freetier")


def test_a_url_error_still_reports_its_reason(monkeypatch):
    def boom(*_a, **_k):
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(presets.urllib.request, "urlopen", boom)
    with pytest.raises(ValueError, match="name resolution failed"):
        fetch_preset_text("freetier")


def test_a_body_that_is_not_toml_is_a_value_error(monkeypatch):
    """A CDN error page answers 200 with HTML, and the same promise has to hold for
    it: a ValueError the caller already catches, not a TOMLDecodeError."""

    class _Html(_Response):
        def read(self):
            return b"<html>404 not found</html>"

    monkeypatch.setattr(presets.urllib.request, "urlopen", lambda *a, **k: _Html(None))
    with pytest.raises(ValueError, match="not valid TOML"):
        fetch_preset_text("freetier")


# ── A fetched lineup may not send keys in the clear ──────────────────────────


def _served(text: str):
    class _Ok(_Response):
        def read(self):
            return text.encode()

    return lambda *_a, **_k: _Ok(None)


_HTTPS_PRESET = '[[llms]]\nname="a"\nbase_url="https://a/v1"\nmodel="m"\napi_key_ref="A"\n'


def test_a_fetched_preset_with_a_plaintext_base_url_is_refused(monkeypatch):
    plaintext = _HTTPS_PRESET.replace("https://a/v1", "http://a/v1")
    monkeypatch.setattr(presets.urllib.request, "urlopen", _served(plaintext))
    with pytest.raises(ValueError, match="non-https base_url"):
        fetch_preset_text("freetier")


def test_a_plaintext_provider_in_the_paid_catalog_is_refused(monkeypatch):
    catalog = (
        '[[provider]]\nid="p"\nbase_url="http://p/v1"\napi_key_ref="P"\n'
        '[[provider.models]]\nalias="x"\nmodel="m"\n'
    )
    monkeypatch.setattr(presets.urllib.request, "urlopen", _served(catalog))
    with pytest.raises(ValueError, match="non-https base_url"):
        fetch_preset_text("paid-catalog")


def test_a_fetched_preset_carrying_custom_entries_is_refused_whole(monkeypatch):
    """`[[custom]]` means *the host's own*; a curated lineup declaring one is a
    contradiction, and the pool entry beside it must not slip through either."""
    carrying = _HTTPS_PRESET + (
        '[[custom]]\nname="mine"\nbase_url="https://mine/v1"\nmodel="m"\napi_key_ref="MY_KEY"\n'
    )
    monkeypatch.setattr(presets.urllib.request, "urlopen", _served(carrying))
    with pytest.raises(ValueError, match=r"carries \[\[custom\]\]"):
        fetch_preset_text("freetier")


def test_an_https_preset_passes(monkeypatch):
    monkeypatch.setattr(presets.urllib.request, "urlopen", _served(_HTTPS_PRESET))
    assert fetch_preset_text("freetier") == _HTTPS_PRESET


# ── The precedence ───────────────────────────────────────────────────────────


@pytest.fixture
def offline(monkeypatch):
    def _fail(_name: str) -> str:
        raise ValueError("offline")

    monkeypatch.setattr(presets, "fetch_preset_text", _fail)


def _cached(home, name: str, text: str) -> None:
    path = home / "presets" / f"{name}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_a_successful_fetch_overwrites_the_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(presets, "fetch_preset_text", lambda _name: "fresh")
    _cached(tmp_path, "freetier", "stale")
    assert PresetSource(tmp_path).text("freetier") == "fresh"
    assert (tmp_path / "presets" / "freetier.toml").read_text() == "fresh"


def test_a_failed_fetch_falls_back_to_the_cache(tmp_path, offline):
    _cached(tmp_path, "freetier", "what this machine last saw")
    assert PresetSource(tmp_path).text("freetier") == "what this machine last saw"


def test_the_wheels_copy_is_the_floor_under_both(tmp_path, offline, bundled_presets):
    assert "[[llms]]" in PresetSource(tmp_path).text("freetier")


def test_the_floor_is_dropped_where_an_entry_would_move_backwards(
    tmp_path, offline, bundled_presets
):
    """The copy in the wheel is older than the repository by construction, so a read
    that decides where a stored entry should point may not reach it."""
    with pytest.raises(ValueError, match="offline"):
        PresetSource(tmp_path).text("freetier", floor=False)


def test_nothing_at_all_is_an_error(tmp_path, offline):
    with pytest.raises(ValueError, match="offline"):
        PresetSource(tmp_path).text("freetier")


def test_prefer_cache_does_not_go_to_the_network(tmp_path, monkeypatch):
    """A declared alias resolves on the request path: provisioning must not wait on
    the network when a copy is already here."""

    def _boom(_name: str) -> str:
        raise AssertionError("the cached copy was already here")

    monkeypatch.setattr(presets, "fetch_preset_text", _boom)
    _cached(tmp_path, "paid-catalog", "cached catalog")
    assert PresetSource(tmp_path).text("paid-catalog", prefer_cache=True) == "cached catalog"


def test_prefer_cache_fetches_when_there_is_no_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(presets, "fetch_preset_text", lambda _name: "fetched")
    assert PresetSource(tmp_path).text("paid-catalog", prefer_cache=True) == "fetched"


def test_refresh_raises_and_leaves_the_previous_copy(tmp_path, offline):
    """The caller decides whether the copy already here is good enough."""
    _cached(tmp_path, "paid-catalog", "previous")
    with pytest.raises(ValueError, match="offline"):
        PresetSource(tmp_path).refresh("paid-catalog")
    assert (tmp_path / "presets" / "paid-catalog.toml").read_text() == "previous"


def test_nowhere_writable_still_reads_and_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(presets, "fetch_preset_text", lambda _name: "fetched")
    assert PresetSource().text("freetier") == "fetched"
    assert list(tmp_path.iterdir()) == []
