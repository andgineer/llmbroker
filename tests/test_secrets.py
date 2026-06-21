"""Tests for secrets batteries and the broker's private key resolution."""

import asyncio

import llmbroker
import llmbroker.sqlite
import pytest
from llmbroker.models import LLMConfig
from llmbroker.registry import Registry as FileRegistry
from llmbroker.secrets import (
    DictSecrets,
    MutableSecretsProtocol,
    Secrets,
    UserScopeError,
    as_secrets,
)


def test_env_secrets_resolves(monkeypatch):
    monkeypatch.setenv("MY_KEY", "secret-value")
    assert asyncio.run(Secrets().resolve("MY_KEY")) == "secret-value"


def test_env_secrets_missing_raises(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    with pytest.raises(KeyError):
        asyncio.run(Secrets().resolve("NOPE"))


def test_dict_secrets_resolves():
    assert asyncio.run(DictSecrets({"K": "v"}).resolve("K")) == "v"


def test_dict_secrets_missing_raises():
    with pytest.raises(KeyError):
        asyncio.run(DictSecrets({}).resolve("K"))


def test_callable_adapter_sync():
    secrets = as_secrets(lambda ref: f"resolved-{ref}")
    assert asyncio.run(secrets.resolve("X")) == "resolved-X"


def test_read_only_batteries_are_not_mutable():
    assert not isinstance(Secrets(), MutableSecretsProtocol)
    assert not isinstance(DictSecrets({}), MutableSecretsProtocol)


def test_sqlite_secrets_round_trip(tmp_path):
    db = str(tmp_path / "b.db")
    secrets = llmbroker.sqlite.Secrets(db)

    async def run():
        await secrets.set("K", "v")
        return await secrets.resolve("K")

    assert asyncio.run(run()) == "v"
    assert isinstance(secrets, MutableSecretsProtocol)


def test_sqlite_secrets_missing_raises(tmp_path):
    secrets = llmbroker.sqlite.Secrets(str(tmp_path / "b.db"))
    with pytest.raises(KeyError):
        asyncio.run(secrets.resolve("MISSING"))


def test_broker_resolves_key_not_on_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "the-secret")
    toml = tmp_path / "llms.toml"
    toml.write_text(
        '[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="MY_API_KEY"\n',
    )

    async def run():
        broker = llmbroker.AsyncBroker(registry=FileRegistry(toml))
        async with broker:
            await broker.ensure_pool()
            cfg = (await broker.get("p1")).config
            assert cfg.api_key_ref == "MY_API_KEY"
            assert "the-secret" not in (cfg.api_key_ref, cfg.base_url, cfg.model, cfg.name)
            # the resolved key lives only in the private map
            assert broker._resolved_keys["p1"] == "the-secret"

    asyncio.run(run())


def test_seed_seeds_secret_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SEED_KEY", "from-env")
    db = str(tmp_path / "b.db")
    src = tmp_path / "llms.toml"
    src.write_text(
        '[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="SEED_KEY"\n',
    )

    async def run():
        secrets = llmbroker.sqlite.Secrets(db)
        broker = llmbroker.AsyncBroker(
            registry=llmbroker.sqlite.Registry(db),
            secrets=secrets,
            seed=FileRegistry(src),
            seed_policy=llmbroker.SeedPolicy.MIRROR,
        )
        async with broker:
            return await secrets.resolve("SEED_KEY")

    assert asyncio.run(run()) == "from-env"


def test_seed_preserves_existing_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("SEED_KEY", "from-env")
    db = str(tmp_path / "b.db")
    src = tmp_path / "llms.toml"
    src.write_text(
        '[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="SEED_KEY"\n',
    )

    async def run():
        secrets = llmbroker.sqlite.Secrets(db)
        await secrets.set("SEED_KEY", "admin-edited")
        broker = llmbroker.AsyncBroker(
            registry=llmbroker.sqlite.Registry(db),
            secrets=secrets,
            seed=FileRegistry(src),
            seed_policy=llmbroker.SeedPolicy.MIRROR,
        )
        async with broker:
            return await secrets.resolve("SEED_KEY")

    assert asyncio.run(run()) == "admin-edited"


def test_missing_ref_with_readonly_secrets_does_not_block(tmp_path, monkeypatch):
    monkeypatch.delenv("ABSENT_KEY", raising=False)
    db = str(tmp_path / "b.db")
    src = tmp_path / "llms.toml"
    src.write_text(
        '[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="ABSENT_KEY"\n',
    )

    async def run():
        broker = llmbroker.AsyncBroker(
            registry=llmbroker.sqlite.Registry(db),
            seed=FileRegistry(src),
            seed_policy=llmbroker.SeedPolicy.MIRROR,
        )
        async with broker:
            await broker.get("p1")
            return True

    assert asyncio.run(run()) is True


def test_llm_config_dataclass_has_no_secret_field():
    cfg = LLMConfig(name="p", base_url="u", model="m", api_key_ref="REF")
    assert not hasattr(cfg, "api_key")


# ── per-user scoping tests ────────────────────────────────────────────────────


def test_sqlite_secrets_two_users_isolated(tmp_path):
    """resolve/set round-trip under two distinct user_ids stays isolated."""
    db = str(tmp_path / "b.db")
    secrets = llmbroker.sqlite.Secrets(db)

    async def run():
        await secrets.set("K", "alice-val", "alice")
        await secrets.set("K", "bob-val", "bob")
        assert await secrets.resolve("K", "alice") == "alice-val"
        assert await secrets.resolve("K", "bob") == "bob-val"

    asyncio.run(run())


def test_sqlite_secrets_user_id_none_resolves_unscoped(tmp_path):
    """user_id=None resolves the NULL-scoped (unscoped) row."""
    db = str(tmp_path / "b.db")
    secrets = llmbroker.sqlite.Secrets(db)

    async def run():
        await secrets.set("K", "global-val")
        return await secrets.resolve("K")

    assert asyncio.run(run()) == "global-val"


def test_sqlite_secrets_missing_per_user_raises_key_error(tmp_path):
    """A missing per-user row raises KeyError, never falls back to shared row."""
    db = str(tmp_path / "b.db")
    secrets = llmbroker.sqlite.Secrets(db)

    async def run():
        await secrets.set("K", "global-val")  # NULL-scoped
        await secrets.resolve("K", "alice")  # alice has no row

    with pytest.raises(KeyError):
        asyncio.run(run())


def test_secrets_require_user_id_raises_on_none(monkeypatch):
    """Secrets(require_user_id=True) raises UserScopeError when user_id is None."""
    monkeypatch.setenv("K", "val")
    s = Secrets(require_user_id=True)
    with pytest.raises(UserScopeError):
        asyncio.run(s.resolve("K", None))


def test_secrets_require_user_id_resolves_with_user(monkeypatch):
    """Secrets(require_user_id=True) resolves normally when user_id is provided."""
    monkeypatch.setenv("K", "val")
    s = Secrets(require_user_id=True)
    assert asyncio.run(s.resolve("K", "alice")) == "val"


def test_sqlite_secrets_require_user_id_raises_on_none(tmp_path, monkeypatch):
    """sqlite.Secrets(require_user_id=True) raises UserScopeError when user_id is None."""
    db = str(tmp_path / "b.db")
    secrets = llmbroker.sqlite.Secrets(db, require_user_id=True)
    with pytest.raises(UserScopeError):
        asyncio.run(secrets.resolve("K", None))


def test_sqlite_secrets_require_user_id_set_raises_on_none(tmp_path):
    """sqlite.Secrets(require_user_id=True).set raises UserScopeError when user_id is None."""
    db = str(tmp_path / "b.db")
    secrets = llmbroker.sqlite.Secrets(db, require_user_id=True)
    with pytest.raises(UserScopeError):
        asyncio.run(secrets.set("K", "val", None))
