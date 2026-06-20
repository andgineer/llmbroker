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
            await broker.ensure_started()
            cfg = broker["p1"].config
            assert cfg.api_key_ref == "MY_API_KEY"
            assert "the-secret" not in (cfg.api_key_ref, cfg.base_url, cfg.model, cfg.name)
            # the resolved key lives only in the private map
            assert broker._resolved_keys["p1"] == "the-secret"

    asyncio.run(run())


def test_sync_configs_seeds_secret_from_env(tmp_path, monkeypatch, real_broker_sync):  # noqa: ARG001
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
        )
        async with broker:
            await broker.sync_configs(FileRegistry(src), policy="mirror")
            return await secrets.resolve("SEED_KEY")

    assert asyncio.run(run()) == "from-env"


def test_sync_configs_preserves_existing_secret(tmp_path, monkeypatch, real_broker_sync):  # noqa: ARG001
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
        )
        async with broker:
            await broker.sync_configs(FileRegistry(src), policy="mirror")
            return await secrets.resolve("SEED_KEY")

    assert asyncio.run(run()) == "admin-edited"


def test_missing_ref_with_readonly_secrets_does_not_block(tmp_path, monkeypatch, real_broker_sync):  # noqa: ARG001
    monkeypatch.delenv("ABSENT_KEY", raising=False)
    db = str(tmp_path / "b.db")
    src = tmp_path / "llms.toml"
    src.write_text(
        '[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="ABSENT_KEY"\n',
    )

    async def run():
        broker = llmbroker.AsyncBroker(registry=llmbroker.sqlite.Registry(db))
        async with broker:
            # read-only env secrets + missing var: sync still completes, key just unresolved
            await broker.sync_configs(FileRegistry(src), policy="mirror")
            return "p1" in broker

    assert asyncio.run(run()) is True


def test_llm_config_dataclass_has_no_secret_field():
    cfg = LLMConfig(name="p", base_url="u", model="m", api_key_ref="REF")
    assert not hasattr(cfg, "api_key")
