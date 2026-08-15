"""Tests for the Optimizer: backoff bookkeeping, quality windows, and demotion verdicts.

Dead-key-drop / journal-rebuild behavior lives in ``Learner`` — see
``tests/test_learning.py``.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from llmbroker.broker.broker import AsyncBroker
from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.models import LLMConfig
from llmbroker.optimizer import Optimizer, wilson_upper
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import InMemoryStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(name: str = "x") -> LLMConfig:
    return LLMConfig(name=name, base_url="https://x/v1", model="m", api_key_ref="k")


def _registry(tmp_path, name="p1"):
    f = tmp_path / "llms.toml"
    f.write_text(f'[[llms]]\nname="{name}"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')
    return Registry(f)


# ---------------------------------------------------------------------------
# Optimizer unit tests: backoff bookkeeping
# ---------------------------------------------------------------------------


def test_rl_fail_count_increments_on_rate_limit():
    opt = Optimizer()
    opt.on_rate_limited("x")
    opt.on_rate_limited("x")
    assert opt.rl_fail_count("x") == 2


def test_success_resets_rl_fail_count():
    opt = Optimizer()
    opt._rl_fail_count["x"] = 2
    opt.on_success("x")
    assert opt.rl_fail_count("x") == 0


# ---------------------------------------------------------------------------
# wilson_upper() + quality windows / demotion verdicts
# ---------------------------------------------------------------------------


def test_wilson_upper_all_good_high_bound():
    bound = wilson_upper([1.0] * 30, 1.96)
    assert bound == pytest.approx(1.0, abs=1e-4)


def test_wilson_upper_all_bad_low_bound():
    bound = wilson_upper([0.0] * 30, 1.96)
    assert bound < 0.2


def test_is_demoted_false_below_min_count():
    opt = Optimizer(quality_min_count=10)
    for i in range(9):
        opt.record_quality("x", None, f"c{i}", 0.0)
    assert opt.is_demoted("x", None) is False


def test_is_demoted_true_once_min_count_reached_with_bad_scores():
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    for i in range(10):
        opt.record_quality("x", None, f"c{i}", 0.0)
    assert opt.is_demoted("x", None) is True


def test_is_demoted_false_for_unknown_model():
    opt = Optimizer()
    assert opt.is_demoted("ghost", None) is False


def test_window_evicts_oldest_beyond_quality_window():
    opt = Optimizer(quality_window=5, quality_min_count=1, quality_floor=0.5)
    for i, score in enumerate((0.0, 0.0, 0.0, 0.0, 0.0)):
        opt.record_quality("x", None, f"bad{i}", score)
    assert opt.is_demoted("x", None) is True
    for i in range(5):
        opt.record_quality("x", None, f"good{i}", 1.0)  # evicts every 0.0
    assert opt.is_demoted("x", None) is False


def test_demoted_operations_only_lists_demoted_ops():
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    for i in range(10):
        opt.record_quality("x", "bad_op", f"c{i}", 0.0)
        opt.record_quality("x", "good_op", f"c{i}", 1.0)
    assert opt.demoted_operations("x") == frozenset({"bad_op"})


def test_record_quality_logs_flip_warning_then_info(caplog):
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    with caplog.at_level("WARNING", logger="llmbroker.broker"):
        for i in range(10):
            opt.record_quality("x", "op", f"bad{i}", 0.0)
    demote_msg = next(r.message for r in caplog.records if "quality-demoted" in r.message)
    assert "wilson upper" in demote_msg
    assert "< floor 0.30" in demote_msg

    caplog.clear()
    with caplog.at_level("INFO", logger="llmbroker.broker"):
        for i in range(10):
            opt.record_quality("x", "op", f"good{i}", 1.0)
    clear_msg = next(r.message for r in caplog.records if "demotion cleared" in r.message)
    assert "wilson upper" in clear_msg


def test_load_scores_replaces_windows_wholesale():
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    opt.load_scores({("x", "op"): [(f"c{i}", 0.0) for i in range(10)]})
    assert opt.is_demoted("x", "op") is True
    opt.load_scores({("x", "op"): [(f"c{i}", 1.0) for i in range(10)]})
    assert opt.is_demoted("x", "op") is False


def test_load_scores_truncates_to_quality_window():
    """Values arrive oldest-first, so the tail kept is the newest rated calls."""
    opt = Optimizer(quality_window=3, quality_min_count=1)
    loaded = [(f"old{i}", 0.0) for i in range(3)] + [(f"new{i}", 1.0) for i in range(3)]
    opt.load_scores({("x", "op"): loaded})
    assert list(opt._scores[("x", "op")]) == [("new0", 1.0), ("new1", 1.0), ("new2", 1.0)]


def test_re_rating_a_call_replaces_its_entry_instead_of_adding_one():
    """A host changing its mind is not a second observation: the window is keyed by
    the call, so ten verdicts on one answer stay one vote and cannot demote."""
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    for _ in range(10):
        opt.record_quality("x", "op", "the-one-call", 0.0)
    assert list(opt._scores[("x", "op")]) == [("the-one-call", 0.0)]
    assert opt.is_demoted("x", "op") is False


def test_re_rating_a_call_keeps_its_place_in_the_window():
    """The entry ages with the call it rates, not with the verdict, so a late change
    of mind does not push an older call out of the window."""
    opt = Optimizer(quality_window=3, quality_min_count=1)
    for name in ("a", "b", "c"):
        opt.record_quality("x", "op", name, 0.0)
    opt.record_quality("x", "op", "a", 1.0)
    assert list(opt._scores[("x", "op")]) == [("a", 1.0), ("b", 0.0), ("c", 0.0)]


# ---------------------------------------------------------------------------
# Router and Broker wiring tests
# ---------------------------------------------------------------------------


def test_broker_no_optimizer_when_optimize_false(tmp_path):
    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path),
            store=InMemoryStore(),
            optimize=False,
            sync=None,
        ) as broker:
            assert broker._optimizer is None
            assert broker._router._optimizer is None

    asyncio.run(run())


def test_broker_demoted_model_still_serves_as_last_resort(tmp_path):
    """A quality-demoted model with no alternative is still acquired — soft demotion."""

    async def run():
        opt = Optimizer(quality_min_count=10, quality_floor=0.3)
        for i in range(10):
            opt.record_quality("p1", None, f"c{i}", 0.0)
        async with AsyncBroker(
            registry=_registry(tmp_path),
            secrets=DictSecrets({"K": "test"}),
            store=InMemoryStore(),
            optimize=opt,
            sync=None,
        ) as broker:
            picked = await broker._pool.acquire(0, payable=frozenset({"K"}), operation=None)
            assert picked.name == "p1"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Underprovisioned alert tests
# ---------------------------------------------------------------------------


def _make_unavailable(broker, name: str) -> None:
    """Simulate a cooling LLM without going through a real cool_down cycle."""
    slot = broker._pool._slots[name]
    slot.cooldown_until = datetime.now(UTC) + timedelta(seconds=300)
    slot.fail_count = 1


def _timeout_exc() -> NoLLMAvailableError:
    return NoLLMAvailableError("test", reason="timeout")


def test_underprovisioned_alert_when_all_cooling(tmp_path, caplog):
    """_maybe_alert_underprov fires when all keyed pool members are not AVAILABLE."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path),
            secrets=DictSecrets({"K": "test"}),
            store=InMemoryStore(),
            optimize=True,
            sync=None,
        ) as broker:
            _make_unavailable(broker, "p1")
            with caplog.at_level("WARNING", logger="llmbroker.broker"):
                broker._maybe_alert_underprov(_timeout_exc())
            assert any("under-provisioned" in r.message for r in caplog.records)

    asyncio.run(run())


def test_underprov_alert_fires_despite_keyless_config_present(tmp_path, caplog):
    """Regression for the has_key filter: a keyless config (always AVAILABLE by default)
    must not mask the alarm when every *keyed* config is COOLING."""

    async def run():
        f = tmp_path / "llms.toml"
        f.write_text(
            '[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n'
            '[[llms]]\nname="p2"\nbase_url="https://y/v1"\nmodel="m"\napi_key_ref="UNRESOLVED"\n',
        )
        async with AsyncBroker(
            registry=Registry(f),
            secrets=DictSecrets({"K": "test"}),  # p2's ref is left unresolved — stays keyless
            store=InMemoryStore(),
            optimize=True,
            sync=None,
        ) as broker:
            assert broker._pool.config("p2").api_key_ref not in broker._catalog.payable
            _make_unavailable(broker, "p1")
            with caplog.at_level("WARNING", logger="llmbroker.broker"):
                broker._maybe_alert_underprov(_timeout_exc())
            assert any("under-provisioned" in r.message for r in caplog.records)

    asyncio.run(run())


def test_no_underprov_alert_when_some_available(tmp_path, caplog):
    """No alert when at least one keyed LLM is AVAILABLE."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path, name="p1"),
            secrets=DictSecrets({"K": "test"}),
            store=InMemoryStore(),
            optimize=True,
            sync=None,
        ) as broker:
            # p1 stays AVAILABLE (default)
            with caplog.at_level("WARNING", logger="llmbroker.broker"):
                broker._maybe_alert_underprov(_timeout_exc())
            assert not any("under-provisioned" in r.message for r in caplog.records)

    asyncio.run(run())


def test_no_underprov_alert_when_optimize_false(tmp_path, caplog):
    """No alert when optimize=False (no optimizer attached)."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path),
            store=InMemoryStore(),
            optimize=False,
            sync=None,
        ) as broker:
            _make_unavailable(broker, "p1")
            with caplog.at_level("WARNING", logger="llmbroker.broker"):
                broker._maybe_alert_underprov(_timeout_exc())
            assert not any("under-provisioned" in r.message for r in caplog.records)

    asyncio.run(run())


def test_underprov_alert_debounced(tmp_path, caplog):
    """Two consecutive calls within the debounce interval produce only one alert."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path),
            secrets=DictSecrets({"K": "test"}),
            store=InMemoryStore(),
            optimize=True,
            sync=None,
        ) as broker:
            _make_unavailable(broker, "p1")
            with caplog.at_level("WARNING", logger="llmbroker.broker"):
                broker._maybe_alert_underprov(_timeout_exc())
                broker._maybe_alert_underprov(_timeout_exc())  # within interval
            hits = [r for r in caplog.records if "under-provisioned" in r.message]
            assert len(hits) == 1

    asyncio.run(run())


def test_underprov_alert_via_ask_wiring(tmp_path, caplog):
    """ask() catches NoLLMAvailableError and calls _maybe_alert_underprov() via try/except."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path),
            secrets=DictSecrets({"K": "test"}),
            store=InMemoryStore(),
            optimize=True,
            sync=None,
        ) as broker:
            # Occupy the only slot so the next acquire raises TimeoutError → NoLLMAvailableError.
            # _make_unavailable marks p1 non-AVAILABLE so _maybe_alert_underprov fires.
            await broker._pool.acquire(0, payable=frozenset({"K"}))
            _make_unavailable(broker, "p1")

            with (
                caplog.at_level("WARNING", logger="llmbroker.broker"),
                pytest.raises(
                    NoLLMAvailableError,
                ),
            ):
                await broker.ask("hi", wait=0)

            assert any("under-provisioned" in r.message for r in caplog.records)

    asyncio.run(run())
