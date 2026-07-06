"""Cheap-at-low-usage (mission #8): a healthy pool never reads the journal on the
success path, and even with optimization on, only the one provision-time warm
start touches it — not one read per call.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from llmbroker.broker.broker import AsyncBroker
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import InMemoryStore

_PATCH = "llmbroker.broker.router.call_provider"


def _registry(tmp_path, name="p1"):
    f = tmp_path / "llms.toml"
    f.write_text(f'[[llms]]\nname="{name}"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')
    return Registry(f)


class CountingStore:
    """Wraps InMemoryStore, counting record()/calls() invocations."""

    def __init__(self, inner: InMemoryStore) -> None:
        self._inner = inner
        self.record_calls = 0
        self.calls_invocations = 0

    async def record(self, call) -> None:
        self.record_calls += 1
        await self._inner.record(call)

    async def record_quality(self, llm_name, operation, score, *, call_id=None) -> None:
        await self._inner.record_quality(llm_name, operation, score, call_id=call_id)

    async def calls(self, *, limit: int, scope: str | None = None) -> list:
        self.calls_invocations += 1
        return []

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def test_optimize_false_never_reads_journal_and_records_every_call(tmp_path):
    async def run():
        store = CountingStore(InMemoryStore())
        async with AsyncBroker(
            registry=_registry(tmp_path),
            secrets=DictSecrets({"K": "test"}),
            store=store,
            optimize=False,
        ) as broker:
            with patch(_PATCH, new=AsyncMock(return_value=("ok", None, None))):
                for _ in range(5):
                    await broker.ask("hi")

        assert store.calls_invocations == 0
        assert store.record_calls == 5

    asyncio.run(run())


def test_optimize_true_reads_journal_exactly_once_for_warm_start(tmp_path):
    async def run():
        store = CountingStore(InMemoryStore())
        async with AsyncBroker(
            registry=_registry(tmp_path),
            secrets=DictSecrets({"K": "test"}),
            store=store,
            optimize=True,
        ) as broker:
            with patch(_PATCH, new=AsyncMock(return_value=("ok", None, None))):
                for _ in range(5):
                    await broker.ask("hi")

        assert store.calls_invocations == 1
        assert store.record_calls == 5

    asyncio.run(run())
