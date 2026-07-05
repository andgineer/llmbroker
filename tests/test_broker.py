"""Tests for AsyncBroker core routing, add/remove, error escalation."""

import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import llmbroker.sqlite
import pytest

from llmbroker.broker import AsyncBroker
from llmbroker.exceptions import AllLLMsFailedError, NoLLMAvailableError
from llmbroker.models import LifecyclePhase, LLMConfig, SeedPolicy
from llmbroker.standalone.registry import Registry as FileRegistry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.telemetry import NoTelemetry


def _registry(tmp_path, entries=None):
    if entries is None:
        entries = [("p1", "https://x/v1", "m", "K")]
    lines = []
    for name, base_url, model, ref in entries:
        lines += [
            "[[llms]]",
            f'name="{name}"',
            f'base_url="{base_url}"',
            f'model="{model}"',
            f'api_key_ref="{ref}"',
        ]
    f = tmp_path / "llms.toml"
    f.write_text("\n".join(lines) + "\n")
    return FileRegistry(f)


def _http_ok(content="hello"):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=False)
    cm.post = AsyncMock(return_value=resp)
    return cm


def _http_error(status):
    mock_request = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = status
    mock_response.headers = {}
    mock_response.text = f"HTTP {status}"
    exc = httpx.HTTPStatusError("err", request=mock_request, response=mock_response)
    resp = MagicMock()
    resp.raise_for_status.side_effect = exc
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=False)
    cm.post = AsyncMock(return_value=resp)
    return cm


def test_ensure_pool_populates_configs(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            assert await broker.count() == 1
            assert (await broker.get("p1")).config.name == "p1"

    asyncio.run(run())


def test_ensure_pool_idempotent(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            await broker.ensure_pool()  # second call after __aenter__ — must be no-op
            assert await broker.count() == 1

    asyncio.run(run())


def test_snapshot_names(tmp_path):
    async def run():
        entries = [("a", "https://a/v1", "m", "K"), ("b", "https://b/v1", "m", "K")]
        async with AsyncBroker(
            registry=_registry(tmp_path, entries), telemetry=NoTelemetry()
        ) as broker:
            assert set((await broker.snapshot()).keys()) == {"a", "b"}

    asyncio.run(run())


def test_get_returns_async_llm_with_correct_config(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            llm = await broker.get("p1")
            assert llm.config.name == "p1"
            assert llm.config.base_url == "https://x/v1"

    asyncio.run(run())


def test_get_missing_raises_key_error(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            with pytest.raises(KeyError):
                await broker.get("nope")

    asyncio.run(run())


def test_async_llm_state_available(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            state = await (await broker.get("p1")).state()
            assert state.phase is LifecyclePhase.AVAILABLE

    asyncio.run(run())


def test_async_llm_metrics_no_queryable_telemetry(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            metrics = await (await broker.get("p1")).metrics()
            assert metrics.call_count == 0

    asyncio.run(run())


def _secrets() -> DictSecrets:
    return DictSecrets({"K": "test"})


def test_chat_happy_path(tmp_path):
    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path), secrets=_secrets(), telemetry=NoTelemetry()
        ) as broker:
            with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_ok("world")):
                result = await broker.chat([{"role": "user", "content": "hi"}])
                assert result.text == "world"

    asyncio.run(run())


def test_ask_delegates_to_chat(tmp_path):
    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path), secrets=_secrets(), telemetry=NoTelemetry()
        ) as broker:
            with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_ok("yes")):
                result = await broker.ask("prompt")
                assert result.text == "yes"

    asyncio.run(run())


def test_chat_missing_key_raises_all_llms_failed(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            with pytest.raises(AllLLMsFailedError, match="api_key_ref"):
                await broker.chat([{"role": "user", "content": "hi"}])

    asyncio.run(run())


def test_chat_429_wait0_raises_no_llm_available(tmp_path):
    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path), secrets=_secrets(), telemetry=NoTelemetry()
        ) as broker:
            with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_error(429)):
                with pytest.raises(NoLLMAvailableError):
                    await broker.chat([{"role": "user", "content": "hi"}], wait=0)

    asyncio.run(run())


def test_chat_429_increments_fail_count(tmp_path):
    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path), secrets=_secrets(), telemetry=NoTelemetry()
        ) as broker:
            with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_error(429)):
                with pytest.raises(NoLLMAvailableError):
                    await broker.chat([{"role": "user", "content": "hi"}], wait=0)
            state = await (await broker.get("p1")).state()
            assert state.fail_count == 1
            assert state.phase is LifecyclePhase.COOLING

    asyncio.run(run())


def test_chat_500_wait0_raises_no_llm_available(tmp_path):
    """A generic HTTP error cools the slot and fails over rather than raising AllLLMsFailedError;
    with wait=0 and no other LLM to fail over to, that surfaces as NoLLMAvailableError."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path), secrets=_secrets(), telemetry=NoTelemetry()
        ) as broker:
            with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_error(500)):
                with pytest.raises(NoLLMAvailableError):
                    await broker.chat([{"role": "user", "content": "hi"}], wait=0)

    asyncio.run(run())


def test_chat_empty_pool_wait0_raises_no_llm_available(tmp_path):
    async def run():
        f = tmp_path / "empty.toml"
        f.write_text("")
        async with AsyncBroker(registry=FileRegistry(f), telemetry=NoTelemetry()) as broker:
            with pytest.raises(NoLLMAvailableError):
                await broker.chat([{"role": "user", "content": "hi"}], wait=0)

    asyncio.run(run())


def test_result_record_quality_does_not_raise(tmp_path):
    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path), secrets=_secrets(), telemetry=NoTelemetry()
        ) as broker:
            with patch("llmbroker.chat.httpx.AsyncClient", return_value=_http_ok("hi")):
                result = await broker.chat([{"role": "user", "content": "x"}])
                await result.record_quality(1.0)

    asyncio.run(run())


def test_add_with_readonly_registry_raises(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            with pytest.raises(TypeError, match="does not support mutations"):
                await broker.add(LLMConfig(name="p2", base_url="u", model="m", api_key_ref="K"))

    asyncio.run(run())


def test_add_duplicate_raises_value_error(tmp_path):
    async def run():
        db = str(tmp_path / "b.db")
        async with AsyncBroker(
            registry=llmbroker.sqlite.Registry(db), telemetry=NoTelemetry()
        ) as broker:
            cfg = LLMConfig(name="p1", base_url="https://x/v1", model="m", api_key_ref="K")
            await broker.add(cfg)
            with pytest.raises(ValueError, match="already exists"):
                await broker.add(cfg)

    asyncio.run(run())


def test_update_absent_raises_key_error(tmp_path):
    async def run():
        db = str(tmp_path / "b.db")
        async with AsyncBroker(
            registry=llmbroker.sqlite.Registry(db), telemetry=NoTelemetry()
        ) as broker:
            cfg = LLMConfig(name="ghost", base_url="https://x/v1", model="m", api_key_ref="K")
            with pytest.raises(KeyError):
                await broker.update(cfg)

    asyncio.run(run())


def test_update_changes_config_without_extra_slot(tmp_path):
    async def run():
        db = str(tmp_path / "b.db")
        async with AsyncBroker(
            registry=llmbroker.sqlite.Registry(db), telemetry=NoTelemetry()
        ) as broker:
            original = LLMConfig(name="p1", base_url="https://x/v1", model="m", api_key_ref="K")
            await broker.add(original)
            slot_count_before = len(broker._pool)

            updated = LLMConfig(name="p1", base_url="https://new/v1", model="m2", api_key_ref="K")
            await broker.update(updated)

            assert (await broker.get("p1")).config.base_url == "https://new/v1"
            assert len(broker._pool) == slot_count_before  # no extra slot created

    asyncio.run(run())


def test_seed_with_readonly_registry_raises(tmp_path):
    other = tmp_path / "other.toml"
    other.write_text('[[llms]]\nname="p2"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path),
            seed=FileRegistry(other),
            telemetry=NoTelemetry(),
        ):
            pass

    with pytest.raises(TypeError, match="does not support mutations"):
        asyncio.run(run())


def test_calls_without_queryable_telemetry_raises(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            with pytest.raises(TypeError, match="queryable"):
                await broker.calls(limit=10)

    asyncio.run(run())


def test_purge_calls_without_queryable_telemetry_raises(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            with pytest.raises(TypeError, match="queryable"):
                await broker.purge_calls(before=datetime.now(UTC))

    asyncio.run(run())


def test_snapshot_returns_entry_per_llm(tmp_path):
    async def run():
        entries = [("a", "https://a/v1", "m", "K"), ("b", "https://b/v1", "m", "K")]
        async with AsyncBroker(
            registry=_registry(tmp_path, entries), telemetry=NoTelemetry()
        ) as broker:
            snap = await broker.snapshot()
            assert set(snap) == {"a", "b"}
            assert snap["a"].config.name == "a"

    asyncio.run(run())


# ── constructor seed tests ────────────────────────────────────────────────────


def _toml_registry(tmp_path, name="p1"):
    f = tmp_path / "seed.toml"
    f.write_text(f'[[llms]]\nname="{name}"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')
    return FileRegistry(f)


def test_constructor_seed_if_empty_seeds_on_first_ensure_pool(tmp_path):
    """seed=toml, seed_policy=IF_EMPTY: registry populated on first ensure_pool."""

    async def run():
        db = str(tmp_path / "b.db")
        broker = AsyncBroker(
            registry=llmbroker.sqlite.Registry(db),
            seed=_toml_registry(tmp_path),
            seed_policy=SeedPolicy.IF_EMPTY,
            telemetry=NoTelemetry(),
        )
        async with broker:
            assert (await broker.get("p1")).config.name == "p1"

    asyncio.run(run())


def test_constructor_seed_second_ensure_pool_is_noop(tmp_path, caplog):
    """A second ensure_pool() call is a no-op — no extra warnings emitted."""

    async def run():
        db = str(tmp_path / "b.db")
        broker = AsyncBroker(
            registry=llmbroker.sqlite.Registry(db),
            seed=_toml_registry(tmp_path),
            seed_policy=SeedPolicy.IF_EMPTY,
            telemetry=NoTelemetry(),
        )
        async with broker:
            caplog.clear()
            await broker.ensure_pool()

    with caplog.at_level(logging.WARNING, logger="llmbroker.broker"):
        asyncio.run(run())
    unresolved = [r for r in caplog.records if "could not be resolved" in r.message]
    assert unresolved == []


def test_aenter_seeds_eagerly(tmp_path):
    """async with AsyncBroker(..., seed=...) seeds before any call."""

    async def run():
        db = str(tmp_path / "b.db")
        broker = AsyncBroker(
            registry=llmbroker.sqlite.Registry(db),
            seed=_toml_registry(tmp_path),
            seed_policy=SeedPolicy.IF_EMPTY,
            telemetry=NoTelemetry(),
        )
        async with broker:
            assert await broker.count() == 1
            assert (await broker.get("p1")).config.name == "p1"

    asyncio.run(run())


def test_constructor_seed_policy_mirror_reconciles(tmp_path):
    """seed_policy=MIRROR reconciles the sqlite registry to the toml seed on first init."""

    async def run():
        db = str(tmp_path / "b.db")
        sqlite_reg = llmbroker.sqlite.Registry(db)
        extra = LLMConfig(name="extra", base_url="https://e/v1", model="m", api_key_ref="K")
        await sqlite_reg.add(extra)

        broker = AsyncBroker(
            registry=sqlite_reg,
            seed=_toml_registry(tmp_path),
            seed_policy=SeedPolicy.MIRROR,
            telemetry=NoTelemetry(),
        )
        async with broker:
            assert (await broker.get("p1")).config.name == "p1"
            with pytest.raises(KeyError):
                await broker.get("extra")

    asyncio.run(run())


def test_constructor_seed_policy_add_preserves_existing(tmp_path):
    """seed_policy=ADD adds new entries but leaves existing ones untouched."""

    async def run():
        db = str(tmp_path / "b.db")
        sqlite_reg = llmbroker.sqlite.Registry(db)
        extra = LLMConfig(name="extra", base_url="https://e/v1", model="m", api_key_ref="K")
        await sqlite_reg.add(extra)

        broker = AsyncBroker(
            registry=sqlite_reg,
            seed=_toml_registry(tmp_path),
            seed_policy=SeedPolicy.ADD,
            telemetry=NoTelemetry(),
        )
        async with broker:
            assert (await broker.get("p1")).config.name == "p1"
            assert (await broker.get("extra")).config.name == "extra"

    asyncio.run(run())


def test_constructor_seed_policy_add_does_not_overwrite(tmp_path):
    """seed_policy=ADD does not update a config that already exists by name."""

    async def run():
        db = str(tmp_path / "b.db")
        sqlite_reg = llmbroker.sqlite.Registry(db)
        original = LLMConfig(name="p1", base_url="https://original/v1", model="m", api_key_ref="K")
        await sqlite_reg.add(original)

        broker = AsyncBroker(
            registry=sqlite_reg,
            seed=_toml_registry(tmp_path),  # seed also has p1 but at https://x/v1
            seed_policy=SeedPolicy.ADD,
            telemetry=NoTelemetry(),
        )
        async with broker:
            assert (await broker.get("p1")).config.base_url == "https://original/v1"

    asyncio.run(run())


# ── per-user scoping tests ────────────────────────────────────────────────────


def test_two_scopes_have_isolated_pool(tmp_path):
    """Two brokers with different scope over one SQLite registry see separate configs.

    The registry itself still scopes by ``user_id`` internally (Plan 3 removes that
    parameter); the broker just forwards its ``scope`` string through.
    """

    async def run():
        db = str(tmp_path / "b.db")
        reg_a = llmbroker.sqlite.Registry(db)
        reg_b = llmbroker.sqlite.Registry(db)

        cfg_a = LLMConfig(name="llm", base_url="https://a/v1", model="m", api_key_ref="KA")
        cfg_b = LLMConfig(name="llm", base_url="https://b/v1", model="m", api_key_ref="KB")
        await reg_a.add(cfg_a, "alice")
        await reg_b.add(cfg_b, "bob")

        broker_a = AsyncBroker(
            registry=llmbroker.sqlite.Registry(db),
            scope="alice",
            telemetry=NoTelemetry(),
        )
        broker_b = AsyncBroker(
            registry=llmbroker.sqlite.Registry(db),
            scope="bob",
            telemetry=NoTelemetry(),
        )
        async with broker_a, broker_b:
            assert (await broker_a.get("llm")).config.base_url == "https://a/v1"
            assert (await broker_b.get("llm")).config.base_url == "https://b/v1"

    asyncio.run(run())


def test_scope_none_reproduces_single_tenant_behavior(tmp_path):
    """scope=None (default) is equivalent to single-tenant behavior."""

    async def run():
        db = str(tmp_path / "b.db")
        reg = llmbroker.sqlite.Registry(db)
        await reg.add(LLMConfig(name="p1", base_url="https://x/v1", model="m", api_key_ref="K"))

        async with AsyncBroker(
            registry=llmbroker.sqlite.Registry(db), telemetry=NoTelemetry()
        ) as broker:
            assert (await broker.get("p1")).config.name == "p1"

    asyncio.run(run())


def test_seed_from_unscoped_source_writes_to_scope(tmp_path):
    """Seeding from an unscoped (file) source writes entries under scope, not under NULL."""

    async def run():
        db = str(tmp_path / "b.db")
        async with AsyncBroker(
            registry=llmbroker.sqlite.Registry(db),
            seed=_toml_registry(tmp_path),
            telemetry=NoTelemetry(),
            scope="alice",
        ):
            pass
        reg = llmbroker.sqlite.Registry(db)
        alice_rows = await reg.load(user_id="alice")
        none_rows = await reg.load()
        bob_rows = await reg.load(user_id="bob")
        assert len(alice_rows) == 1 and alice_rows[0].name == "p1"
        assert none_rows == []
        assert bob_rows == []

    asyncio.run(run())


def test_two_scopes_have_isolated_secrets(tmp_path):
    """Two brokers with different scope resolve different values for the same api_key_ref,
    via the scope-prefixed ref (own key), falling back to the shared ref."""

    async def run():
        db = str(tmp_path / "b.db")
        secrets = llmbroker.sqlite.Secrets(db)
        await secrets.set("alice/KEY", "alice-secret")
        await secrets.set("bob/KEY", "bob-secret")

        cfg = LLMConfig(name="llm", base_url="https://x/v1", model="m", api_key_ref="KEY")
        reg = llmbroker.sqlite.Registry(db)
        await reg.add(cfg, "alice")
        await reg.add(cfg, "bob")

        broker_a = AsyncBroker(
            registry=llmbroker.sqlite.Registry(db),
            secrets=secrets,
            scope="alice",
            telemetry=NoTelemetry(),
        )
        broker_b = AsyncBroker(
            registry=llmbroker.sqlite.Registry(db),
            secrets=secrets,
            scope="bob",
            telemetry=NoTelemetry(),
        )
        async with broker_a, broker_b:
            assert broker_a._pool.resolved_key("llm") == "alice-secret"
            assert broker_b._pool.resolved_key("llm") == "bob-secret"

    asyncio.run(run())


def test_scope_without_own_key_falls_back_to_shared_ref(tmp_path):
    """A scope with no own-prefixed secret falls back to the shared (unprefixed) ref."""

    async def run():
        db = str(tmp_path / "b.db")
        secrets = llmbroker.sqlite.Secrets(db)
        await secrets.set("KEY", "shared-secret")

        cfg = LLMConfig(name="llm", base_url="https://x/v1", model="m", api_key_ref="KEY")
        reg = llmbroker.sqlite.Registry(db)
        await reg.add(cfg, "alice")

        async with AsyncBroker(
            registry=llmbroker.sqlite.Registry(db),
            secrets=secrets,
            scope="alice",
            telemetry=NoTelemetry(),
        ) as broker:
            assert broker._pool.resolved_key("llm") == "shared-secret"

    asyncio.run(run())
