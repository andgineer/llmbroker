"""Tests for the manual disable latch: warm start, live round trip, remove/readd.

Uses sqlite (registry + store disabled-map) for determinism/speed.
"""

from llmbroker.broker import presets
from llmbroker.broker.broker import AsyncBroker
from llmbroker.models import Call, CallStatus, LLMConfig
from llmbroker.optimizer import Optimizer
from llmbroker.sqlite import Store as SqliteStore
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.standalone.secrets import DictSecrets


def _lineup(name: str) -> str:
    return f'[[llms]]\nname="{name}"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n'


def _cfg(name: str = "p1") -> LLMConfig:
    return LLMConfig(name=name, base_url="https://x/v1", model="m", api_key_ref="K", synced=True)


def _call(call_id: str, name: str = "p1", operation: str | None = None) -> Call:
    return Call(id=call_id, llm_name=name, operation=operation, trace_id=None, status=CallStatus.OK)


async def test_persisted_manual_latch_applied_at_provision(tmp_path):
    """The disabled map warm-starts at provision via the journal-rebuild path."""
    db = str(tmp_path / "b.db")
    reg = SqliteRegistry(db)
    await reg.mirror([_cfg()])
    await SqliteStore(db).set_disabled("p1", True)

    async with AsyncBroker(
        registry=SqliteRegistry(db),
        secrets=DictSecrets({"K": "key"}),
        store=SqliteStore(db),
        sync=None,
    ) as broker:
        assert broker._pool.is_disabled("p1")


async def test_disable_enable_llm_round_trip_preserves_quality_window(tmp_path):
    """No quality reset on re-enable (user decision) — the window survives untouched."""
    db = str(tmp_path / "b.db")
    reg = SqliteRegistry(db)
    await reg.mirror([_cfg()])
    opt = Optimizer(quality_min_count=4, quality_confidence=0.8)

    async with AsyncBroker(
        registry=SqliteRegistry(db),
        secrets=DictSecrets({"K": "key"}),
        store=SqliteStore(db),
        optimize=opt,
        sync=None,
    ) as broker:
        for i in range(5):
            call = _call(f"c{i}", operation="summarize")
            await broker._store.record(call)
            await broker.record_quality("p1", "summarize", 0.0)
        assert len(opt._scores[("p1", "summarize")]) == 5

        await broker.disable_llm("p1")
        assert broker._pool.is_disabled("p1")
        assert await SqliteStore(db).get_disabled("p1") is True
        assert (await broker.get("p1")).disabled is True
        snapshot = await broker.snapshot()
        assert snapshot["p1"].disabled == (await broker.get("p1")).disabled

        await broker.enable_llm("p1")
        assert not broker._pool.is_disabled("p1")
        assert len(opt._scores[("p1", "summarize")]) == 5
        assert await SqliteStore(db).get_disabled("p1") is False
        assert (await broker.get("p1")).disabled is False
        snapshot = await broker.snapshot()
        assert snapshot["p1"].disabled == (await broker.get("p1")).disabled


async def test_disable_enable_llm_without_optimizer_still_persists(tmp_path):
    """disable_llm/enable_llm write the disabled map even with optimize=False — only the
    provision-time warm start needs the learner, not the admin write path."""
    db = str(tmp_path / "b.db")
    reg = SqliteRegistry(db)
    await reg.mirror([_cfg()])

    async with AsyncBroker(
        registry=SqliteRegistry(db),
        secrets=DictSecrets({"K": "key"}),
        store=SqliteStore(db),
        optimize=False,
        sync=None,
    ) as broker:
        assert broker._optimizer is None

        await broker.disable_llm("p1")
        assert broker._pool.is_disabled("p1")
        assert await SqliteStore(db).get_disabled("p1") is True

        await broker.enable_llm("p1")
        assert not broker._pool.is_disabled("p1")
        assert await SqliteStore(db).get_disabled("p1") is False


async def test_remove_then_readd_same_name_is_routable_after_disable(tmp_path, monkeypatch):
    """Regression: removing a benched model used to leave the pool's stale benched
    latch behind, so a fresh config re-added under the same name was silently stuck
    non-routable — no error, just never acquirable."""
    db = str(tmp_path / "b.db")
    reg = SqliteRegistry(db)
    await reg.mirror([_cfg()])
    served = {"text": _lineup("p1")}
    monkeypatch.setattr(presets, "fetch_preset_text", lambda _name: served["text"])

    async with AsyncBroker(
        registry=SqliteRegistry(db),
        secrets=DictSecrets({"K": "key"}),
        store=SqliteStore(db),
        sync=None,
    ) as broker:
        await broker.disable_llm("p1")
        assert broker._pool.is_disabled("p1")

        # A same-key arrival is what pays for a removal, so this is how an entry
        # actually leaves the registry — and then comes back under its old name.
        served["text"] = _lineup("p2")
        await broker.sync("freetier")
        assert "p1" not in broker._pool

        served["text"] = _lineup("p1")
        await broker.sync("freetier")
        assert not broker._pool.is_disabled("p1")
        picked = await broker._pool.acquire(0)
        assert picked.name == "p1"
