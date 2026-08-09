"""Tests for secrets batteries and the broker's private key resolution."""

import asyncio

import aioboto3
import hvac
import llmbroker
import pytest

from llmbroker.aws import Secrets as AwsSecrets
from llmbroker.broker import presets
from llmbroker.models import LLMConfig
from llmbroker.mongodb import Secrets as MongoSecrets
from llmbroker.postgres import Secrets as PostgresSecrets
from llmbroker.protocols.secrets import MutableSecretsProtocol
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.sqlite import Secrets as SqliteSecrets
from llmbroker.standalone.registry import Registry as FileRegistry
from llmbroker.standalone.secrets import DictSecrets, Secrets, as_secrets
from llmbroker.vault import Secrets as VaultSecrets


def _vault_delete_recursive(client: hvac.Client, path: str, mount_point: str = "secret") -> None:
    try:
        result = client.secrets.kv.v2.list_secrets(path=path, mount_point=mount_point)
    except hvac.exceptions.InvalidPath:
        return
    for key in result["data"]["keys"]:
        full = f"{path}{key}"
        if key.endswith("/"):
            _vault_delete_recursive(client, full, mount_point)
        else:
            client.secrets.kv.v2.delete_metadata_and_all_versions(
                path=full, mount_point=mount_point
            )


async def _aws_purge(url: str) -> None:
    session = aioboto3.Session()
    async with session.client(
        "secretsmanager", region_name="us-east-1", endpoint_url=url
    ) as client:
        paginator = client.get_paginator("list_secrets")
        async for page in paginator.paginate():
            for secret in page["SecretList"]:
                await client.delete_secret(SecretId=secret["ARN"], ForceDeleteWithoutRecovery=True)


def test_env_secrets_resolves(monkeypatch):
    monkeypatch.setenv("MY_KEY", "secret-value")
    assert asyncio.run(Secrets().resolve("MY_KEY")) == "secret-value"


def test_env_secrets_missing_raises(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    with pytest.raises(KeyError):
        asyncio.run(Secrets().resolve("NOPE"))


def test_env_secrets_empty_value_counts_as_unset(monkeypatch):
    monkeypatch.setenv("BLANK_KEY", "")
    with pytest.raises(KeyError):
        asyncio.run(Secrets().resolve("BLANK_KEY"))


def test_env_secrets_whitespace_value_counts_as_unset(monkeypatch):
    monkeypatch.setenv("BLANK_KEY", "   \t ")
    with pytest.raises(KeyError):
        asyncio.run(Secrets().resolve("BLANK_KEY"))


def test_env_file_fills_in_for_a_blank_export(tmp_path, monkeypatch):
    monkeypatch.setenv("BLANK_KEY", "")
    env = tmp_path / ".env"
    env.write_text("BLANK_KEY=from-file\n")
    assert asyncio.run(Secrets(env).resolve("BLANK_KEY")) == "from-file"


def test_env_file_whitespace_value_counts_as_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("BLANK_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text('BLANK_KEY="   "\n')
    with pytest.raises(KeyError):
        asyncio.run(Secrets(env).resolve("BLANK_KEY"))


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
    secrets = SqliteSecrets(db)

    async def run():
        await secrets.set("K", "v")
        return await secrets.resolve("K")

    assert asyncio.run(run()) == "v"
    assert isinstance(secrets, MutableSecretsProtocol)


def test_sqlite_secrets_missing_raises(tmp_path):
    secrets = SqliteSecrets(str(tmp_path / "b.db"))
    with pytest.raises(KeyError):
        asyncio.run(secrets.resolve("MISSING"))


def _serve(monkeypatch, ref: str) -> None:
    """Serve a one-entry curated lineup to ``sync("freetier")``, keyed by ``ref``."""
    monkeypatch.setattr(
        presets,
        "fetch_preset_text",
        lambda _name: (
            f'[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="{ref}"\n'
        ),
    )


def test_broker_resolves_key_not_on_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "the-secret")
    # A registry the host brought itself journals into ./store under the CWD.
    monkeypatch.chdir(tmp_path)
    toml = tmp_path / "llms.toml"
    toml.write_text(
        '[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="MY_API_KEY"\n',
    )

    async def run():
        broker = llmbroker.AsyncBroker(registry=FileRegistry(toml), sync=None)
        async with broker:
            await broker.ensure_pool()
            cfg = (await broker.get("p1")).config
            assert cfg.api_key_ref == "MY_API_KEY"
            assert "the-secret" not in (cfg.api_key_ref, cfg.base_url, cfg.model, cfg.name)
            # the resolved key lives only in the pool's private slot
            assert broker._pool.resolved_key("p1") == "the-secret"

    asyncio.run(run())


def test_seed_seeds_secret_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SEED_KEY", "from-env")
    monkeypatch.chdir(tmp_path)
    _serve(monkeypatch, "SEED_KEY")
    db = str(tmp_path / "b.db")

    async def run():
        secrets = SqliteSecrets(db)
        broker = llmbroker.AsyncBroker(
            registry=SqliteRegistry(db),
            secrets=secrets,
            sync=None,
        )
        await broker.sync("freetier")
        async with broker:
            return await secrets.resolve("SEED_KEY")

    assert asyncio.run(run()) == "from-env"


def test_seed_preserves_existing_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("SEED_KEY", "from-env")
    monkeypatch.chdir(tmp_path)
    _serve(monkeypatch, "SEED_KEY")
    db = str(tmp_path / "b.db")

    async def run():
        secrets = SqliteSecrets(db)
        await secrets.set("SEED_KEY", "admin-edited")
        broker = llmbroker.AsyncBroker(
            registry=SqliteRegistry(db),
            secrets=secrets,
            sync=None,
        )
        await broker.sync("freetier")
        async with broker:
            return await secrets.resolve("SEED_KEY")

    assert asyncio.run(run()) == "admin-edited"


def test_missing_ref_with_readonly_secrets_does_not_block(tmp_path, monkeypatch):
    monkeypatch.delenv("ABSENT_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    _serve(monkeypatch, "ABSENT_KEY")
    db = str(tmp_path / "b.db")

    async def run():
        broker = llmbroker.AsyncBroker(registry=SqliteRegistry(db), sync=None)
        await broker.sync("freetier")
        async with broker:
            await broker.get("p1")
            return True

    assert asyncio.run(run()) is True


def test_llm_config_dataclass_has_no_secret_field():
    cfg = LLMConfig(name="p", base_url="u", model="m", api_key_ref="REF")
    assert not hasattr(cfg, "api_key")


# ── Parametrized backend tests for MutableSecretsProtocol ────────────────────


@pytest.fixture(
    params=["sqlite", "postgres", "mongodb", "aws", "vault"],
    ids=["sqlite", "postgres", "mongodb", "aws", "vault"],
)
async def mutable_secrets(request, tmp_path_factory, pg_pool, mongo_db):
    param = request.param
    if param == "sqlite":
        db_path = str(tmp_path_factory.mktemp("msec_sqlite") / "sec.db")
        yield SqliteSecrets(db_path)
    elif param == "postgres":
        yield PostgresSecrets(pg_pool)
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM llmbroker_secrets")
    elif param == "mongodb":
        yield MongoSecrets(mongo_db)
        await mongo_db["llmbroker_secrets"].delete_many({})
    elif param == "aws":
        url = request.getfixturevalue("localstack_url")
        yield AwsSecrets(region_name="us-east-1", endpoint_url=url)
        await _aws_purge(url)
    elif param == "vault":
        url, token = request.getfixturevalue("vault_url_and_token")
        yield VaultSecrets(url=url, token=token)
        _vault_delete_recursive(hvac.Client(url=url, token=token), "llmbroker/")


async def test_mutable_set_and_resolve(mutable_secrets):
    await mutable_secrets.set("K", "secret")
    assert await mutable_secrets.resolve("K") == "secret"


async def test_mutable_set_upserts(mutable_secrets):
    await mutable_secrets.set("K", "v1")
    await mutable_secrets.set("K", "v2")
    assert await mutable_secrets.resolve("K") == "v2"


async def test_mutable_resolve_missing_raises_key_error(mutable_secrets):
    with pytest.raises(KeyError):
        await mutable_secrets.resolve("MISSING")
