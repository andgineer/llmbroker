"""The synchronous ``Broker`` is a first-class surface: N host threads share one
background loop, `parallel` still serializes the model, and `close()` reclaims
the loop thread.
"""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import InMemoryStore
from llmbroker.sync import Broker

_PATCH = "llmbroker.broker.router.call_provider"
_THREADS = 6


def _registry(tmp_path, parallel: int | None):
    f = tmp_path / "llms.toml"
    line = f"parallel={parallel}\n" if parallel is not None else ""
    f.write_text(
        f'[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n{line}',
    )
    return Registry(f)


def _broker(tmp_path, parallel: int | None) -> Broker:
    return Broker(
        registry=_registry(tmp_path, parallel),
        secrets=DictSecrets({"K": "test"}),
        store=InMemoryStore(),
    )


class _OverlapWatch:
    """A provider stub that fails the test if two calls are ever in flight at once."""

    def __init__(self) -> None:
        self.max_in_flight = 0
        self._in_flight = 0
        self._lock = threading.Lock()

    async def __call__(self, config, api_key, messages, tools, *, client=None, timeout=None):
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            await asyncio.sleep(0.01)  # long enough for a sibling thread to overlap
            return "ok", None, None
        finally:
            with self._lock:
                self._in_flight -= 1


def test_threads_share_one_broker_and_parallel_1_serializes(tmp_path):
    watch = _OverlapWatch()
    broker = _broker(tmp_path, parallel=1)
    try:
        with patch(_PATCH, new=watch):
            with ThreadPoolExecutor(max_workers=_THREADS) as pool:
                texts = list(pool.map(lambda i: broker.ask(f"q{i}").text, range(_THREADS)))
        assert texts == ["ok"] * _THREADS
        assert watch.max_in_flight == 1
    finally:
        broker.close()


def test_uncapped_model_actually_overlaps_across_threads(tmp_path):
    """The counterpart: without ``parallel`` the same threads do run concurrently,
    so the serialization above is the cap's doing and not the loop's."""
    watch = _OverlapWatch()
    broker = _broker(tmp_path, parallel=None)
    try:
        with patch(_PATCH, new=watch):
            with ThreadPoolExecutor(max_workers=_THREADS) as pool:
                list(pool.map(lambda i: broker.ask(f"q{i}").text, range(_THREADS)))
        assert watch.max_in_flight > 1
    finally:
        broker.close()


def test_close_joins_the_loop_thread(tmp_path):
    broker = _broker(tmp_path, parallel=None)
    with patch(_PATCH, new=_OverlapWatch()):
        broker.ask("hi")
    thread = broker._thread
    broker.close()
    assert not thread.is_alive()
    broker.close()  # idempotent


def test_close_is_prompt(tmp_path):
    """`close()` joins within its own 5s timeout — a hung join is a real regression."""
    broker = _broker(tmp_path, parallel=None)
    with patch(_PATCH, new=_OverlapWatch()):
        broker.ask("hi")
    started = time.monotonic()
    broker.close()
    assert time.monotonic() - started < 5.0
