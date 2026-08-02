"""Pool health: the provider counts, the `degraded` predicate, and the one ERROR
per transition. `snapshot()` and the log read the same measurement, so an admin
UI and an alerting rule can never disagree.
"""

import logging

import pytest

from llmbroker.broker.broker import AsyncBroker
from llmbroker.models import LLMSnapshot
from llmbroker.standalone.registry import Registry as FileRegistry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import InMemoryStore

_ENTRY = '[[llms]]\nname="{name}"\nbase_url="https://{name}/v1"\nmodel="m"\napi_key_ref="{ref}"\n'
_UNPOOLED = _ENTRY.format(name="paid", ref="P") + "pool = false\n"


def _registry(tmp_path, *entries, keys=""):
    target = tmp_path / "llms.toml"
    target.write_text(
        "".join(_ENTRY.format(name=name, ref=ref) for name, ref in entries) + keys,
    )
    return FileRegistry(target)


def _broker(tmp_path, *entries, present=(), keys=""):
    return AsyncBroker(
        registry=_registry(tmp_path, *entries, keys=keys),
        secrets=DictSecrets({ref: "sk" for ref in present}),
        store=InMemoryStore(),
    )


# ── The snapshot is a mapping and carries the pool-wide facts ────────────────


async def test_the_snapshot_is_still_a_mapping_of_name_to_llm_snapshot(tmp_path):
    async with _broker(tmp_path, ("a", "A"), ("b", "B"), present=("A", "B")) as broker:
        snap = await broker.snapshot()
        assert set(snap) == {"a", "b"}
        assert isinstance(snap["a"], LLMSnapshot)
        assert [name for name, _ in snap.items()] == ["a", "b"]
        assert "a" in snap
        assert len(snap) == 2
        assert snap.get("ghost") is None


async def test_two_entries_on_one_ref_count_as_one_provider(tmp_path):
    async with _broker(tmp_path, ("a", "SHARED"), ("b", "SHARED"), present=("SHARED",)) as broker:
        snap = await broker.snapshot()
        assert (snap.providers_usable, snap.providers_total) == (1, 1)
        assert snap.degraded is True


@pytest.mark.parametrize(
    ("present", "usable", "degraded"),
    [((), 0, True), (("A",), 1, True), (("A", "B"), 2, False)],
)
async def test_degraded_at_zero_and_one_usable_provider(tmp_path, present, usable, degraded):
    async with _broker(tmp_path, ("a", "A"), ("b", "B"), present=present) as broker:
        snap = await broker.snapshot()
        assert (snap.providers_usable, snap.providers_total) == (usable, 2)
        assert snap.degraded is degraded


async def test_missing_keys_carry_the_help_text_from_the_keys_table(tmp_path):
    keys = '[keys.B]\nhelp = "get one at example.com"\n'
    async with _broker(tmp_path, ("a", "A"), ("b", "B"), present=("A",), keys=keys) as broker:
        snap = await broker.snapshot()
        assert [(k.api_key_ref, k.help, k.entry_names) for k in snap.missing_keys] == [
            ("B", "get one at example.com", ("b",)),
        ]


async def test_a_ref_one_entry_can_use_is_not_a_missing_key(tmp_path):
    """Two entries on one ref are one quota: the provider is usable, so nothing is
    held back — even though a second entry on it happens to be keyless."""
    async with _broker(tmp_path, ("a", "SHARED"), ("b", "B"), present=("SHARED",)) as broker:
        snap = await broker.snapshot()
        assert [k.api_key_ref for k in snap.missing_keys] == ["B"]


# ── The ERROR, on the transition only ────────────────────────────────────────


def _health_lines(caplog):
    return [
        (r.levelno, r.message)
        for r in caplog.records
        if r.message.startswith(("pool cannot serve", "pool degraded", "pool recovered"))
    ]


async def test_a_healthy_pool_logs_nothing_about_health(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
        async with _broker(tmp_path, ("a", "A"), ("b", "B"), present=("A", "B")) as broker:
            await broker.count()
            await broker._catalog.resync()
    assert _health_lines(caplog) == []


async def test_one_usable_provider_is_an_error_naming_the_missing_ref(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
        async with _broker(tmp_path, ("a", "A"), ("b", "B"), present=("A",)) as broker:
            await broker.count()
    ((level, message),) = _health_lines(caplog)
    assert level == logging.ERROR
    assert "no failover left" in message
    assert "B" in message


async def test_no_usable_provider_says_the_pool_cannot_serve(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
        async with _broker(tmp_path, ("a", "A"), ("b", "B")) as broker:
            await broker.count()
    ((level, message),) = _health_lines(caplog)
    assert level == logging.ERROR
    assert "cannot serve any request" in message


async def test_the_error_is_logged_once_per_transition_not_per_reconcile(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
        async with _broker(tmp_path, ("a", "A"), ("b", "B"), present=("A",)) as broker:
            await broker.count()
            await broker._catalog.resync()
            await broker._catalog.resync()
    assert len(_health_lines(caplog)) == 1


async def test_recovery_logs_exactly_one_info(tmp_path, caplog):
    secrets = DictSecrets({"A": "sk"})
    broker = AsyncBroker(
        registry=_registry(tmp_path, ("a", "A"), ("b", "B")),
        secrets=secrets,
        store=InMemoryStore(),
    )
    with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
        async with broker:
            await broker.count()
            secrets._mapping["B"] = "sk"
            await broker._catalog.resync()
            await broker._catalog.resync()
    levels = [level for level, _ in _health_lines(caplog)]
    assert levels == [logging.ERROR, logging.INFO]
    assert _health_lines(caplog)[1][1] == "pool recovered: 2 of 2 providers usable"


async def test_a_healthy_pool_that_loses_a_provider_alarms(tmp_path, caplog):
    """The transition the ERROR exists for: a lineup change leaves one quota."""
    registry = _registry(tmp_path, ("a", "A"), ("b", "B"))
    broker = AsyncBroker(
        registry=registry,
        secrets=DictSecrets({"A": "sk", "B": "sk"}),
        store=InMemoryStore(),
    )
    with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
        async with broker:
            await broker.count()
            assert _health_lines(caplog) == []
            registry.path.write_text(_ENTRY.format(name="a", ref="A"))
            await broker._catalog.resync()
    ((level, message),) = _health_lines(caplog)
    assert level == logging.ERROR
    assert "1 of 1 providers usable" in message


async def test_the_last_provider_going_is_its_own_alarm(tmp_path, caplog):
    """Both degraded states are ERROR, so keying the alarm on the log level would
    swallow the 1 -> 0 step — the moment the pool stops answering at all."""
    registry = _registry(tmp_path, ("a", "A"), ("b", "B"))
    broker = AsyncBroker(
        registry=registry,
        secrets=DictSecrets({"A": "sk"}),
        store=InMemoryStore(),
    )
    with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
        async with broker:
            await broker.count()
            assert "no failover left" in _health_lines(caplog)[0][1]
            registry.path.write_text(_ENTRY.format(name="b", ref="B"))
            await broker._catalog.resync()
            assert (await broker.snapshot()).providers_usable == 0
    assert [level for level, _ in _health_lines(caplog)] == [logging.ERROR, logging.ERROR]
    assert "cannot serve any request" in _health_lines(caplog)[1][1]


async def test_gaining_a_further_provider_is_not_worth_a_line(tmp_path, caplog):
    """Every healthy count is one state: recovery is news, a fourth quota is not."""
    registry = _registry(tmp_path, ("a", "A"), ("b", "B"))
    secrets = DictSecrets({"A": "sk", "B": "sk"})
    broker = AsyncBroker(registry=registry, secrets=secrets, store=InMemoryStore())
    with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
        async with broker:
            await broker.count()
            registry.path.write_text(
                "".join(
                    _ENTRY.format(name=n, ref=r) for n, r in (("a", "A"), ("b", "B"), ("c", "C"))
                )
            )
            secrets._mapping["C"] = "sk"
            await broker._catalog.resync()
    assert _health_lines(caplog) == []


async def test_a_registry_that_pools_nothing_is_not_a_degraded_pool(tmp_path, caplog):
    """`pool = false` entries never join the pool, so there is nothing to degrade —
    and "no provider has a key" would name a cause that is not the case here."""
    target = tmp_path / "llms.toml"
    target.write_text(_UNPOOLED)
    broker = AsyncBroker(
        registry=FileRegistry(target),
        secrets=DictSecrets({"P": "sk"}),
        store=InMemoryStore(),
    )
    with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
        async with broker:
            await broker.count()
            snap = await broker.snapshot()
    assert (snap.providers_usable, snap.providers_total) == (0, 0)
    assert snap.degraded is False
    assert _health_lines(caplog) == []


async def test_a_pool_that_empties_out_does_not_report_a_recovery(tmp_path, caplog):
    """Losing the last pooled entry is a membership change, not a repair."""
    registry = _registry(tmp_path, ("a", "A"), ("b", "B"))
    broker = AsyncBroker(
        registry=registry,
        secrets=DictSecrets({"A": "sk"}),
        store=InMemoryStore(),
    )
    with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
        async with broker:
            await broker.count()
            assert "no failover left" in _health_lines(caplog)[0][1]
            registry.path.write_text(_UNPOOLED)
            await broker._catalog.resync()
    assert len(_health_lines(caplog)) == 1


async def test_a_revoked_key_deactivates_its_provider(tmp_path, caplog):
    """The reconcile resolved nothing for B, so the pool must stop counting it and
    stop routing at it — not keep the stale value until the journal condemns it."""
    secrets = DictSecrets({"A": "sk", "B": "sk"})
    broker = AsyncBroker(
        registry=_registry(tmp_path, ("a", "A"), ("b", "B")),
        secrets=secrets,
        store=InMemoryStore(),
    )
    with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
        async with broker:
            await broker.count()
            del secrets._mapping["B"]
            await broker._catalog.resync()
            snap = await broker.snapshot()
    assert (snap.providers_usable, snap.providers_total) == (1, 2)
    assert snap["b"].has_key is False
    assert broker._pool.has_key("b") is False
    assert [k.api_key_ref for k in snap.missing_keys] == ["B"]
    assert "no failover left" in _health_lines(caplog)[0][1]


async def test_a_fully_keyed_pool_never_reads_the_key_table(tmp_path):
    """Help text is only ever rendered for a missing key, so the common case must
    cost no registry read at all."""
    registry = _registry(tmp_path, ("a", "A"), ("b", "B"))
    reads = 0
    original = registry.key_info

    async def counted():
        nonlocal reads
        reads += 1
        return await original()

    registry.key_info = counted
    async with AsyncBroker(
        registry=registry,
        secrets=DictSecrets({"A": "sk", "B": "sk"}),
        store=InMemoryStore(),
    ) as broker:
        await broker.count()
        await broker._catalog.resync()
        assert reads == 0
        registry.path.write_text(
            _ENTRY.format(name="a", ref="A") + _ENTRY.format(name="c", ref="C")
        )
        await broker._catalog.resync()
    assert reads == 1
