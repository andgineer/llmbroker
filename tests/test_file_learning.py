"""Confirmed-bug repros: the default FileStore must feed the journal rebuild —
learning, calls(), and peer cooldowns must all survive a restart over the same TOML."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from llmbroker.broker.broker import AsyncBroker
from llmbroker.models import Call, CallStatus, LLMConfig, key_hash
from llmbroker.sqlite import Registry as SqliteRegistry


def _toml(tmp_path, name="m1", ref="FL_KEY"):
    p = tmp_path / "llms.toml"
    p.write_text(
        f'[[llms]]\nname = "{name}"\nbase_url = "https://x/v1"\n'
        f'model = "m"\napi_key_ref = "{ref}"\n'
    )
    return p


async def test_learning_survives_restart_on_default_file_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FL_KEY", "sk-test")
    toml = _toml(tmp_path)
    b1 = AsyncBroker(str(toml))
    await b1.ensure_pool()
    for _ in range(10):  # quality_min_count=10; 10 zeros → wilson upper ≈0.28 < floor 0.3
        await b1._store.record_quality("m1", "summarize", 0.0)
    assert b1._optimizer.is_demoted("m1", "summarize")

    b2 = AsyncBroker(str(toml))  # fresh process over the same TOML
    await b2.ensure_pool()  # warm start must reload windows from store/calls/
    assert b2._optimizer.is_demoted("m1", "summarize")


async def test_calls_works_on_default_file_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FL_KEY", "sk-test")
    toml = _toml(tmp_path)
    b1 = AsyncBroker(str(toml))
    await b1.ensure_pool()
    await b1._store.record_quality("m1", "summarize", 1.0)

    b2 = AsyncBroker(str(toml))
    await b2.ensure_pool()
    rows = await b2.calls(limit=10)
    assert rows
    assert rows[0].kind == "quality"


@pytest.mark.parametrize("store_kind", ["file", "sqlite"])
async def test_two_brokers_converge_over_one_journal(tmp_path, monkeypatch, store_kind):
    monkeypatch.setenv("FL_KEY", "sk-test")
    toml = _toml(tmp_path)

    if store_kind == "file":
        a = AsyncBroker(str(toml))
        b = AsyncBroker(str(toml))
        await a.ensure_pool()
        await b.ensure_pool()
    else:
        db = str(tmp_path / "b.db")
        await SqliteRegistry(db).mirror(
            [LLMConfig(name="m1", base_url="https://x/v1", model="m", api_key_ref="FL_KEY")]
        )
        a = AsyncBroker(db)
        b = AsyncBroker(db)
        await a.sync(str(toml))  # env bootstrap copies the key into the DB secrets
        await a.ensure_pool()
        await b.ensure_pool()

    until = datetime.now(UTC) + timedelta(seconds=60)
    await a._store.record(
        Call(
            id=str(uuid.uuid4()),
            llm_name="m1",
            operation=None,
            trace_id=None,
            status=CallStatus.RATE_LIMITED,
            ts=datetime.now(UTC),
            http_status=429,
            cooldown_until=until,
            key_hash=key_hash("sk-test"),
        )
    )
    for _ in range(10):
        await a._store.record_quality("m1", "summarize", 0.0)

    await b._learning_hook.maybe_rebuild(force=True)
    assert b._optimizer.is_demoted("m1", "summarize")
    assert b._pool._slots["m1"].cooldown_until == until
