"""``parallel`` cap end-to-end (mission #8): concurrent asks through a real
``AsyncBroker`` are capped by the pool, not just at the unit level.
"""

import asyncio
from unittest.mock import patch

from llmbroker.broker.broker import AsyncBroker
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import InMemoryStore

_PATCH = "llmbroker.broker.router.call_provider"


def _registry(tmp_path, parallel: int | None):
    parallel_line = f"parallel={parallel}\n" if parallel is not None else ""
    f = tmp_path / "llms.toml"
    f.write_text(
        f'[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n{parallel_line}',
    )
    return Registry(f)


async def _fire_three_concurrent_asks(tmp_path, parallel: int | None) -> int:
    expected = 3 if parallel is None else min(parallel, 3)
    in_flight = 0
    max_in_flight = 0
    release = asyncio.Event()
    reached = asyncio.Event()  # every concurrently-admissible call has hit its blocking point

    async def fake_call_provider(
        config, api_key, messages, tools, *, client=None, timeout=None, params=None
    ):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        if in_flight >= expected:
            reached.set()
        await release.wait()
        in_flight -= 1
        return "ok", None, None

    async with AsyncBroker(
        registry=_registry(tmp_path, parallel),
        secrets=DictSecrets({"K": "test"}),
        store=InMemoryStore(),
        sync=None,
    ) as broker:
        with patch(_PATCH, new=fake_call_provider):
            tasks = [asyncio.ensure_future(broker.ask("hi")) for _ in range(3)]
            await reached.wait()
            release.set()
            results = await asyncio.gather(*tasks)

    assert all(r.text == "ok" for r in results)
    return max_in_flight


def test_parallel_one_serializes_concurrent_calls(tmp_path):
    max_in_flight = asyncio.run(_fire_three_concurrent_asks(tmp_path, 1))
    assert max_in_flight == 1


def test_parallel_none_allows_full_concurrency(tmp_path):
    max_in_flight = asyncio.run(_fire_three_concurrent_asks(tmp_path, None))
    assert max_in_flight == 3
