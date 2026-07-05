"""Info-log-count invariants for ensure_pool with sqlite registry + secrets.

An unresolved api_key_ref is normal (partial-key framing), so it logs at INFO, not
WARNING — these tests guard against a regression where the same missing-key event
gets logged more than once per resolution attempt.
"""

import asyncio
import logging

import llmbroker
import llmbroker.sqlite

_KEY_REF = "TEST_LLM_SYNC_API_KEY"
_TOML = f'[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="{_KEY_REF}"\n'


def _src_registry(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text(_TOML)
    return llmbroker.Registry(f)


def _broker(db: str) -> llmbroker.AsyncBroker:
    return llmbroker.AsyncBroker(
        registry=llmbroker.sqlite.Registry(db),
        secrets=llmbroker.sqlite.Secrets(db),
    )


async def _seed_db(db: str, tmp_path) -> None:
    """Populate a fresh db's registry once, via an explicit sync() — mirrors the
    one-time DB-init workflow, separate from any later restart/reopen."""
    broker = _broker(db)
    await broker.sync(_src_registry(tmp_path))
    await broker.aclose()


def _info_count(caplog) -> int:
    return sum(1 for r in caplog.records if "not resolved" in r.message)


def test_fresh_db_env_set_zero_info_logs(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(_KEY_REF, "secret")
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "b.db")

    async def run():
        await _seed_db(db, tmp_path)
        async with _broker(db):
            pass

    with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
        asyncio.run(run())
    assert _info_count(caplog) == 0


def test_fresh_db_env_absent_one_info_log(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv(_KEY_REF, raising=False)
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "b.db")

    async def run():
        await _seed_db(db, tmp_path)
        async with _broker(db):
            pass

    with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
        asyncio.run(run())
    assert _info_count(caplog) == 1
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


def test_restart_secret_persisted_zero_info_logs(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv(_KEY_REF, raising=False)
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "b.db")

    async def seed():
        await _seed_db(db, tmp_path)
        secrets = llmbroker.sqlite.Secrets(db)
        await secrets.set(_KEY_REF, "persisted")
        async with _broker(db):
            pass

    asyncio.run(seed())

    async def restart():
        async with _broker(db):
            pass

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
        asyncio.run(restart())
    assert _info_count(caplog) == 0


def test_restart_secret_absent_everywhere_exactly_one_info_log(tmp_path, monkeypatch, caplog):
    """Regression: before the pool-init refactor, two log lines were emitted on restart with missing secret."""
    monkeypatch.delenv(_KEY_REF, raising=False)
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "b.db")

    async def first():
        await _seed_db(db, tmp_path)
        async with _broker(db):
            pass

    asyncio.run(first())

    async def restart():
        async with _broker(db):
            pass

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
        asyncio.run(restart())
    assert _info_count(caplog) == 1


def test_restart_env_set_sqlite_missing_zero_info_logs(tmp_path, monkeypatch, caplog):
    """A newly available env var is picked up on the next explicit sync() — a plain
    restart with no sync() call does not re-bootstrap secrets (sync is explicit now)."""
    monkeypatch.delenv(_KEY_REF, raising=False)
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "b.db")

    async def first():
        await _seed_db(db, tmp_path)
        async with _broker(db):
            pass

    asyncio.run(first())

    monkeypatch.setenv(_KEY_REF, "from-env")

    async def restart():
        broker = _broker(db)
        await broker.sync(_src_registry(tmp_path))
        async with broker:
            pass

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
        asyncio.run(restart())
    assert _info_count(caplog) == 0
