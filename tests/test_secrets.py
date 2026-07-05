"""Tests for secrets batteries and the broker's private key resolution."""

import asyncio

import aioboto3
import hvac
import llmbroker
import llmbroker.aws
import llmbroker.mongodb
import llmbroker.postgres
import llmbroker.sqlite
import llmbroker.vault
import pytest

from llmbroker.exceptions import UserScopeError
from llmbroker.models import LLMConfig
from llmbroker.protocols.secrets import MutableSecretsProtocol
from llmbroker.standalone.registry import Registry as FileRegistry
from llmbroker.standalone.secrets import DictSecrets, Secrets, as_secrets


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
            # the resolved key lives only in the pool's private slot
            assert broker._pool.resolved_key("p1") == "the-secret"

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
        )
        await broker.sync(FileRegistry(src))
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
        )
        await broker.sync(FileRegistry(src))
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
        broker = llmbroker.AsyncBroker(registry=llmbroker.sqlite.Registry(db))
        await broker.sync(FileRegistry(src))
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


# ── Parametrized backend tests for MutableSecretsProtocol ────────────────────


@pytest.fixture(
    params=["sqlite", "postgres", "mongodb", "aws", "vault"],
    ids=["sqlite", "postgres", "mongodb", "aws", "vault"],
)
async def mutable_secrets(request, tmp_path_factory, pg_pool, mongo_db):
    param = request.param
    if param == "sqlite":
        db_path = str(tmp_path_factory.mktemp("msec_sqlite") / "sec.db")
        yield llmbroker.sqlite.Secrets(db_path)
    elif param == "postgres":
        yield llmbroker.postgres.Secrets(pg_pool)
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM llmbroker_secrets")
    elif param == "mongodb":
        yield llmbroker.mongodb.Secrets(mongo_db)
        await mongo_db["llmbroker_secrets"].delete_many({})
    elif param == "aws":
        url = request.getfixturevalue("localstack_url")
        yield llmbroker.aws.Secrets(region_name="us-east-1", endpoint_url=url)
        await _aws_purge(url)
    elif param == "vault":
        url, token = request.getfixturevalue("vault_url_and_token")
        yield llmbroker.vault.Secrets(url=url, token=token)
        _vault_delete_recursive(hvac.Client(url=url, token=token), "llmbroker/")


@pytest.fixture(
    params=["sqlite", "postgres", "mongodb", "aws", "vault"],
    ids=["sqlite", "postgres", "mongodb", "aws", "vault"],
)
async def strict_mutable_secrets(request, tmp_path_factory, pg_pool, mongo_db):
    """Secrets backends constructed with require_user_id=True."""
    param = request.param
    if param == "sqlite":
        db_path = str(tmp_path_factory.mktemp("ssec_sqlite") / "sec.db")
        yield llmbroker.sqlite.Secrets(db_path, require_user_id=True)
    elif param == "postgres":
        yield llmbroker.postgres.Secrets(pg_pool, require_user_id=True)
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM llmbroker_secrets")
    elif param == "mongodb":
        yield llmbroker.mongodb.Secrets(mongo_db, require_user_id=True)
        await mongo_db["llmbroker_secrets"].delete_many({})
    elif param == "aws":
        url = request.getfixturevalue("localstack_url")
        yield llmbroker.aws.Secrets(region_name="us-east-1", endpoint_url=url, require_user_id=True)
        await _aws_purge(url)
    elif param == "vault":
        url, token = request.getfixturevalue("vault_url_and_token")
        yield llmbroker.vault.Secrets(url=url, token=token, require_user_id=True)
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


async def test_mutable_two_users_isolated(mutable_secrets):
    await mutable_secrets.set("K", "alice-val", "alice")
    await mutable_secrets.set("K", "bob-val", "bob")
    assert await mutable_secrets.resolve("K", "alice") == "alice-val"
    assert await mutable_secrets.resolve("K", "bob") == "bob-val"


async def test_mutable_user_id_none_resolves_unscoped(mutable_secrets):
    await mutable_secrets.set("K", "global-val")
    assert await mutable_secrets.resolve("K") == "global-val"


async def test_mutable_missing_per_user_raises_key_error(mutable_secrets):
    await mutable_secrets.set("K", "global-val")
    with pytest.raises(KeyError):
        await mutable_secrets.resolve("K", "alice")


async def test_strict_require_user_id_none_raises_on_resolve(strict_mutable_secrets):
    with pytest.raises(UserScopeError):
        await strict_mutable_secrets.resolve("K", None)


async def test_strict_require_user_id_none_raises_on_set(strict_mutable_secrets):
    with pytest.raises(UserScopeError):
        await strict_mutable_secrets.set("K", "v", None)


async def test_strict_require_user_id_resolves_with_user(strict_mutable_secrets):
    await strict_mutable_secrets.set("K", "val", "alice")
    assert await strict_mutable_secrets.resolve("K", "alice") == "val"
