"""Warning-count invariants for ensure_pool with constructor seed, sqlite registry + secrets."""

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


def _broker(db: str, tmp_path) -> llmbroker.AsyncBroker:
    return llmbroker.AsyncBroker(
        registry=llmbroker.sqlite.Registry(db),
        secrets=llmbroker.sqlite.Secrets(db),
        seed=_src_registry(tmp_path),
        seed_policy=llmbroker.SeedPolicy.ADD,
    )


def _warn_count(caplog) -> int:
    return sum(1 for r in caplog.records if "could not be resolved" in r.message)


def test_fresh_db_env_set_zero_warnings(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(_KEY_REF, "secret")
    db = str(tmp_path / "b.db")

    async def run():
        async with _broker(db, tmp_path):
            pass

    with caplog.at_level(logging.WARNING, logger="llmbroker.broker"):
        asyncio.run(run())
    assert _warn_count(caplog) == 0


def test_fresh_db_env_absent_one_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv(_KEY_REF, raising=False)
    db = str(tmp_path / "b.db")

    async def run():
        async with _broker(db, tmp_path):
            pass

    with caplog.at_level(logging.WARNING, logger="llmbroker.broker"):
        asyncio.run(run())
    assert _warn_count(caplog) == 1


def test_restart_secret_persisted_zero_warnings(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv(_KEY_REF, raising=False)
    db = str(tmp_path / "b.db")

    async def seed():
        secrets = llmbroker.sqlite.Secrets(db)
        await secrets.set(_KEY_REF, "persisted")
        async with _broker(db, tmp_path):
            pass

    asyncio.run(seed())

    async def restart():
        async with _broker(db, tmp_path):
            pass

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="llmbroker.broker"):
        asyncio.run(restart())
    assert _warn_count(caplog) == 0


def test_restart_secret_absent_everywhere_exactly_one_warning(tmp_path, monkeypatch, caplog):
    """Regression: before the pool-init refactor, two warnings were emitted on restart with missing secret."""
    monkeypatch.delenv(_KEY_REF, raising=False)
    db = str(tmp_path / "b.db")

    async def first():
        async with _broker(db, tmp_path):
            pass

    asyncio.run(first())

    async def restart():
        async with _broker(db, tmp_path):
            pass

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="llmbroker.broker"):
        asyncio.run(restart())
    assert _warn_count(caplog) == 1


def test_restart_env_set_sqlite_missing_zero_warnings(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv(_KEY_REF, raising=False)
    db = str(tmp_path / "b.db")

    async def first():
        async with _broker(db, tmp_path):
            pass

    asyncio.run(first())

    monkeypatch.setenv(_KEY_REF, "from-env")

    async def restart():
        async with _broker(db, tmp_path):
            pass

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="llmbroker.broker"):
        asyncio.run(restart())
    assert _warn_count(caplog) == 0
