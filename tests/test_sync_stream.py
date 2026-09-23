"""The synchronous routed stream: each pull relays one pull of the async stream on the
broker's loop, so a host thread gets the same deltas and the same exceptions at the
same points — and closing it, or dropping it unclosed, cancels the provider request."""

import asyncio
import gc
import subprocess
import sys
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from llmbroker import sync as sync_module
from llmbroker.exceptions import (
    NoLLMAvailableError,
    StreamInterruptedError,
    StreamReplacementError,
)
from llmbroker.models import CallStatus
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import FileStore
from llmbroker.sync import Broker, Result, Stream

_SSE_HEADERS = {"content-type": "text/event-stream"}


def _delta(text: str) -> bytes:
    return b'data: {"choices": [{"delta": {"content": "%s"}}]}\n\n' % text.encode()


def _sse(*deltas: str) -> bytes:
    usage = b'data: {"choices": [], "usage": {"total_tokens": 7}}\n\n'
    return b"".join(_delta(d) for d in deltas) + usage + b"data: [DONE]\n\n"


async def _until(event: threading.Event | None) -> None:
    while event is not None and not event.is_set():
        await asyncio.sleep(0.005)


class _Body(httpx.AsyncByteStream):
    """One provider response: waits for ``gate`` (set by the test's thread), sends its
    chunks and, with ``hold``, keeps the response open until the client closes it."""

    def __init__(
        self,
        *chunks: bytes,
        gate: threading.Event | None = None,
        hold: bool = False,
    ) -> None:
        self._chunks = chunks
        self._gate = gate
        self._hold = hold
        self.closed = threading.Event()

    async def __aiter__(self):
        await _until(self._gate)
        for chunk in self._chunks:
            yield chunk
        while self._hold:
            await asyncio.sleep(0.01)

    async def aclose(self) -> None:
        self.closed.set()


def _registry(tmp_path, *names: str, parallel: int | None = None) -> Registry:
    cap = f"parallel={parallel}\n" if parallel is not None else ""
    f = tmp_path / "llms.toml"
    f.write_text(
        "".join(
            f'[[llms]]\nname="{n}"\nbase_url="https://{n}/v1"\nmodel="m"\napi_key_ref="K"\n{cap}'
            for n in names
        ),
    )
    return Registry(f)


def _broker(tmp_path, handler, *names: str, secrets=None, parallel=None) -> Broker:
    broker = Broker(
        registry=_registry(tmp_path, *names, parallel=parallel),
        secrets=secrets if secrets is not None else DictSecrets({"K": "test"}),
        store=FileStore(tmp_path / "store"),
        sync=None,
    )
    broker._async._router._http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=5.0,
    )
    return broker


def _answers(*deltas: str):
    return lambda _request: httpx.Response(200, content=_sse(*deltas), headers=_SSE_HEADERS)


class _Held:
    """A provider that sends one delta and then keeps every response open."""

    def __init__(self) -> None:
        self.bodies: list[_Body] = []

    def __call__(self, _request: httpx.Request) -> httpx.Response:
        body = _Body(_delta("one"), hold=True)
        self.bodies.append(body)
        return httpx.Response(200, stream=body, headers=_SSE_HEADERS)


# --------------------------------------------------------------------------- #
# deltas and identity
# --------------------------------------------------------------------------- #


def test_a_sync_stream_yields_the_deltas_and_names_what_answered(tmp_path):
    with _broker(tmp_path, _answers("Hel", "lo"), "a") as broker:
        stream = broker.stream("hi", operation="write")
        assert isinstance(stream, Stream)
        assert stream.llm_name is None  # nothing has settled before the first pull
        assert list(stream) == ["Hel", "lo"]
        assert (stream.llm_name, stream.operation) == ("a", "write")
        assert stream.usage is not None
        assert stream.usage.total_tokens == 7
        stream.record_quality(0.9)
        (row,) = broker.calls(limit=10)
    assert (row.id, row.status, row.score) == (stream.call_id, CallStatus.OK, 0.9)


def test_a_scoped_sync_stream_pays_with_its_own_key_and_journals_its_scope(tmp_path):
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["authorization"])
        return httpx.Response(200, content=_sse("ok"), headers=_SSE_HEADERS)

    secrets = DictSecrets({"K": "shared", "alice/K": "alice-key"})
    with _broker(tmp_path, handler, "a", secrets=secrets) as broker:
        alice = broker.for_scope("alice")
        assert "".join(alice.stream("hi")) == "ok"
        (row,) = alice.calls(limit=10)
    assert keys == ["Bearer alice-key"]
    assert row.scope == "alice"


def test_threads_stream_through_one_broker_at_once(tmp_path):
    """The WSGI shape: every request thread streams over the one background loop."""
    with _broker(tmp_path, _answers("x", "y"), "a") as broker:
        with ThreadPoolExecutor(max_workers=6) as pool:
            texts = list(pool.map(lambda _: "".join(broker.stream("hi")), range(12)))
    assert texts == ["xy"] * 12


# --------------------------------------------------------------------------- #
# the same exceptions at the same points
# --------------------------------------------------------------------------- #


def test_nothing_is_raised_before_the_first_pull(tmp_path):
    """Routing starts on the first pull, as it does for the async stream — so a pool
    with no key raises there, not where the stream was made."""
    with _broker(tmp_path, _answers("x"), "a", secrets=DictSecrets({})) as broker:
        stream = broker.stream("hi")
        with pytest.raises(NoLLMAvailableError) as excinfo:
            next(stream)
    assert excinfo.value.reason == "no_keys"


def test_a_death_after_the_first_delta_raises_interrupted_and_the_delta_stands(tmp_path):
    async def body():
        yield _delta("par")
        raise httpx.ReadError("connection dropped")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body(), headers=_SSE_HEADERS)

    seen: list[str] = []
    with _broker(tmp_path, handler, "a", "b") as broker:
        stream = broker.stream("hi")
        with pytest.raises(StreamInterruptedError) as excinfo:
            for delta in stream:
                seen.append(delta)
        (row,) = broker.calls(limit=10)  # no failover onto b past the first delta
    assert seen == ["par"]
    assert excinfo.value.llm_name == stream.llm_name == "a"
    assert row.status is CallStatus.ERROR


def test_a_raced_sync_stream_raises_the_replacement_and_rates_it(tmp_path):
    shown = threading.Event()
    bodies = {
        "a": _Body(_delta("a-partial"), hold=True),
        "b": _Body(_sse("b-one", "b-two"), gate=shown),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=bodies[request.url.host], headers=_SSE_HEADERS)

    seen: list[str] = []
    with _broker(tmp_path, handler, "a", "b") as broker:
        stream = broker.stream("hi", fastest_of=2)
        with pytest.raises(StreamReplacementError) as excinfo:
            for delta in stream:
                seen.append(delta)
                shown.set()
        replaced = excinfo.value
        assert list(stream) == []  # terminal for the iterator
        stream.record_quality(0.8)  # the handle now names the replacement
        stream.close()
        assert bodies["a"].closed.is_set()
        rows = {row.llm_name: row for row in broker.calls(limit=10)}
    assert seen == ["a-partial"]
    assert replaced.streamed_llm_name == "a"
    assert (replaced.replacement.text, replaced.replacement.llm_name) == ("b-oneb-two", "b")
    assert (stream.llm_name, stream.call_id) == ("b", replaced.replacement.call_id)
    assert (rows["b"].status, rows["b"].score) == (CallStatus.OK, 0.8)
    assert rows["a"].status is CallStatus.SUPERSEDED


def test_another_answer_comes_back_as_a_sync_result(tmp_path):
    read = threading.Event()
    bodies = {"a": _Body(_sse("a")), "b": _Body(_sse("b"), gate=read)}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=bodies[request.url.host], headers=_SSE_HEADERS)

    with _broker(tmp_path, handler, "a", "b") as broker:
        with broker.stream("hi", fastest_of=2, wait=5) as stream:
            assert list(stream) == ["a"]
            read.set()
            answer = stream.another()
            assert isinstance(answer, Result)
            assert (answer.text, answer.llm_name) == ("b", "b")
            answer.record_quality(0.5)
            assert stream.another() is None
        rows = {row.llm_name: row for row in broker.calls(limit=10)}
    assert rows["b"].score == 0.5


# --------------------------------------------------------------------------- #
# closing early cancels the provider request
# --------------------------------------------------------------------------- #


def test_close_cancels_the_provider_request_and_settles_it_ok(tmp_path):
    provider = _Held()
    with _broker(tmp_path, provider, "a") as broker:
        stream = broker.stream("hi")
        assert next(stream) == "one"
        stream.close()
        assert provider.bodies[0].closed.is_set()  # close waits for the cancellation
        (row,) = broker.calls(limit=10)
        assert row.status is CallStatus.OK  # walking away is not the model's failure
        assert list(stream) == []
        stream.close()  # idempotent
        with pytest.raises(RuntimeError, match="closed"), stream:
            pass


def test_leaving_the_with_block_cancels_the_provider_request(tmp_path):
    provider = _Held()
    with _broker(tmp_path, provider, "a") as broker:
        with broker.stream("hi") as stream:
            for _delta_text in stream:
                break
        assert provider.bodies[0].closed.is_set()


def test_dropping_an_unclosed_stream_cancels_it_and_frees_its_slot(tmp_path):
    """No close() at all — the garbage collector is the backstop, and the slot it held
    comes back, so a ``parallel=1`` model can serve the next stream."""
    provider = _Held()
    with _broker(tmp_path, provider, "a", parallel=1) as broker:
        stream = broker.stream("hi")
        assert next(stream) == "one"
        del stream
        gc.collect()
        assert provider.bodies[0].closed.wait(timeout=2.0)
        with broker.stream("hi", wait=2) as second:
            assert next(second) == "one"


@pytest.mark.parametrize("scoped", [True, False], ids=["with", "bare"])
def test_a_consumer_generator_closed_early_cancels_the_provider_request(tmp_path, scoped):
    """The WSGI abort: the server closes the response body — a generator wrapping the
    stream — when the client goes away, and that must reach the provider. With ``with``
    inside the generator, its close is the stream's close, so nothing is left to wait for."""
    provider = _Held()
    with _broker(tmp_path, provider, "a") as broker:

        def response_body():
            if scoped:
                with broker.stream("hi") as stream:
                    for delta in stream:
                        yield delta.encode()
            else:
                for delta in broker.stream("hi"):
                    yield delta.encode()

        body = response_body()
        assert next(body) == b"one"
        body.close()
        if scoped:
            assert provider.bodies[0].closed.is_set()
        else:
            assert provider.bodies[0].closed.wait(timeout=2.0)


@pytest.mark.parametrize("pulled", [True, False], ids=["started", "unstarted"])
def test_closing_the_broker_ends_its_open_sync_streams(tmp_path, pulled):
    """An explicit close closes every stream the broker owns, so each one ends, and the
    broker refuses new ones."""
    provider = _Held()
    broker = _broker(tmp_path, provider, "a")
    stream = broker.stream("hi")
    if pulled:
        assert next(stream) == "one"
    broker.close()
    assert [body.closed.is_set() for body in provider.bodies] == ([True] if pulled else [])
    with pytest.raises(StopIteration):
        next(stream)
    with pytest.raises(RuntimeError, match="the stream is closed"), stream:
        pass
    stream.close()
    with pytest.raises(RuntimeError, match="the broker is closed"):
        broker.stream("hi")
    with pytest.raises(RuntimeError, match="the broker is closed"):
        broker.for_scope("alice").stream("hi")


# --------------------------------------------------------------------------- #
# a stream and a scoped caller keep their broker alive
# --------------------------------------------------------------------------- #


def _paused(resume: threading.Event):
    """A provider that sends one delta, waits for ``resume``, then finishes the answer."""

    async def body():
        yield _delta("one")
        await _until(resume)
        yield _sse("two")

    return lambda _request: httpx.Response(200, content=body(), headers=_SSE_HEADERS)


def test_a_stream_from_a_broker_nobody_holds_reads_to_the_end(tmp_path):
    """The one-liner ``list(Broker().stream(...))``: the stream alone keeps the broker."""
    stream = _broker(tmp_path, _answers("one", "two"), "a").stream("hi")
    gc.collect()
    assert list(stream) == ["one", "two"]
    thread = stream._broker._thread
    del stream
    gc.collect()
    thread.join(timeout=5.0)
    assert not thread.is_alive()  # the last holder gone, the broker is torn down


def test_dropping_the_broker_mid_stream_does_not_cut_the_answer(tmp_path):
    broker = _broker(tmp_path, _answers("one", "two", "three"), "a")
    stream = broker.stream("hi")
    assert next(stream) == "one"
    del broker
    gc.collect()
    assert list(stream) == ["two", "three"]
    stream.close()


@pytest.mark.parametrize("caller", ["for_scope", "llms"])
def test_a_caller_from_a_broker_nobody_holds_still_streams(tmp_path, caller):
    broker = _broker(tmp_path, _answers("x", "y"), "a")
    llms = broker.for_scope("alice") if caller == "for_scope" else broker.llms
    del broker
    gc.collect()
    assert "".join(llms.stream("hi")) == "xy"


def test_dropping_the_broker_while_a_reader_waits_in_with_leaves_it_the_whole_answer(tmp_path):
    """The documented pattern, ``with llms.stream(...) as s: for d in s``, blocked on a
    quiet provider when the host drops its broker: the reader keeps the broker, gets the
    rest of the answer, and leaves the ``with`` block."""
    resume = threading.Event()
    holder = {"broker": _broker(tmp_path, _paused(resume), "a")}
    llms = holder["broker"].for_scope("u-1")
    loop_thread = holder["broker"]._thread
    got: list[str] = []
    first = threading.Event()

    def read() -> None:
        with llms.stream("hi", wait=25) as stream:
            for delta in stream:
                got.append(delta)
                first.set()
        got.append("end")

    reader = threading.Thread(target=read, daemon=True)  # a hang must not block teardown
    reader.start()
    assert first.wait(timeout=5.0)
    dropper = threading.Thread(target=holder.clear)
    dropper.start()
    dropper.join(timeout=5.0)
    gc.collect()
    assert loop_thread.is_alive()
    resume.set()
    reader.join(timeout=10.0)
    assert not reader.is_alive()
    assert got == ["one", "two", "end"]
    llms = None
    gc.collect()
    loop_thread.join(timeout=5.0)
    assert not loop_thread.is_alive()


# --------------------------------------------------------------------------- #
# collection is a backstop: it cancels what the broker owned and never blocks
# --------------------------------------------------------------------------- #


async def _collect() -> None:
    gc.collect()


@pytest.mark.parametrize("drop", ["stream-first", "broker-first", "cycle", "cycle-on-loop"])
def test_collecting_a_broker_with_an_abandoned_stream_cancels_it_and_stops_the_loop(
    tmp_path, monkeypatch, drop
):
    """Collection hands the teardown to the loop, whichever thread it runs on: the
    provider request is cancelled and the loop thread ends. No journal row is promised."""
    unraisable: list[object] = []
    monkeypatch.setattr(sys, "unraisablehook", unraisable.append)
    ran_on: list[threading.Thread] = []
    stop_after_teardown = sync_module._stop_after_teardown

    def record(loop, broker):
        ran_on.append(threading.current_thread())
        stop_after_teardown(loop, broker)

    monkeypatch.setattr(sync_module, "_stop_after_teardown", record)
    provider = _Held()
    broker = _broker(tmp_path, provider, "a")
    loop, loop_thread = broker._loop, broker._thread
    stream = broker.stream("hi")
    assert next(stream) == "one"
    gc.disable()  # the cycle must be collected where the test says, not on the way
    try:
        if drop == "stream-first":
            del stream
            assert loop_thread.is_alive()
            del broker
        elif drop == "broker-first":
            del broker
            assert loop_thread.is_alive()
            del stream
        else:
            cycle: list[object] = [broker, stream]
            cycle.append(cycle)
            del broker, stream, cycle
            if drop == "cycle":
                gc.collect()
            else:
                asyncio.run_coroutine_threadsafe(_collect(), loop).result(timeout=5.0)
    finally:
        gc.enable()
    loop_thread.join(timeout=5.0)
    assert not loop_thread.is_alive()
    assert loop.is_closed()
    assert provider.bodies[0].closed.is_set()
    assert ran_on == [loop_thread]
    assert unraisable == []


def test_closing_the_broker_settles_and_journals_its_open_stream(tmp_path):
    provider = _Held()
    broker = _broker(tmp_path, provider, "a")
    loop_thread = broker._thread
    stream = broker.stream("hi")
    assert next(stream) == "one"
    broker.close()
    assert not loop_thread.is_alive()  # close() returns only once the loop has stopped
    assert provider.bodies[0].closed.is_set()
    with _broker(tmp_path, provider, "a") as reopened:
        (row,) = reopened.calls(limit=10)
    assert (row.id, row.status) == (stream.call_id, CallStatus.OK)


def test_a_failed_teardown_after_collection_is_logged(tmp_path, monkeypatch, caplog):
    async def fail() -> None:
        raise RuntimeError("store is gone")

    broker = _broker(tmp_path, _answers("x"), "a")
    loop_thread = broker._thread
    monkeypatch.setattr(broker._async, "aclose", fail)
    del broker
    gc.collect()
    loop_thread.join(timeout=5.0)
    assert not loop_thread.is_alive()
    (record,) = [r for r in caplog.records if "nobody closed" in r.getMessage()]
    assert record.exc_info[1].args == ("store is gone",)


_SCRIPT_PRELUDE = f"""
import gc, os, pathlib, sys, threading, time
sys.path.insert(0, {str(Path(__file__).parent)!r})
from test_sync_stream import _broker, _Held
tmp = pathlib.Path(sys.argv[1])
"""


def _run_script(tmp_path, body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _SCRIPT_PRELUDE + textwrap.dedent(body), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_collection_on_a_thread_holding_the_executor_lock_does_not_wait_for_teardown(
    tmp_path,
):
    """The collector can fire inside ``ThreadPoolExecutor.submit``, which holds the lock
    the teardown's journaling needs: waiting there would hang the whole process."""
    done = _run_script(
        tmp_path,
        """
        from concurrent.futures import thread as executor_thread
        broker = _broker(tmp, _Held(), "a")
        stream = broker.stream("hi")
        assert next(stream) == "one"
        loop_thread = broker._thread
        cycle = [broker, stream]
        cycle.append(cycle)
        del broker, stream, cycle
        gc.disable()
        returned = threading.Event()

        def collect_holding_the_lock():
            with executor_thread._global_shutdown_lock:
                gc.collect()
            returned.set()

        threading.Thread(target=collect_holding_the_lock, daemon=True).start()
        ok = returned.wait(10)
        loop_thread.join(10)
        print("returned", ok, "loop ended", not loop_thread.is_alive(), flush=True)
        os._exit(0 if ok else 1)
        """,
    )
    assert (done.returncode, done.stdout.strip()) == (0, "returned True loop ended True")


@pytest.mark.parametrize("reader", [False, True], ids=["idle", "reader-blocked"])
def test_exiting_the_interpreter_with_a_stream_open_is_silent(tmp_path, reader):
    """Exit runs no teardown: no traceback from a journal write the shut executors
    refuse, and no pending task freed under a loop that no longer runs."""
    blocked_reader = """
        def read_on():  # a server thread still blocked in the stream at exit
            for _delta in stream:
                pass

        threading.Thread(target=read_on, daemon=True).start()
        time.sleep(0.1)
    """
    done = _run_script(
        tmp_path,
        """
        broker = _broker(tmp, _Held(), "a")
        stream = broker.stream("hi")
        assert next(stream) == "one"
        """
        + (blocked_reader if reader else "")
        + """
        print("exiting", flush=True)
        """,
    )
    noise = [line for line in done.stderr.splitlines() if "pool degraded" not in line]
    assert (done.returncode, done.stdout, noise) == (0, "exiting\n", [])


@pytest.mark.parametrize("closer", ["stream", "broker"])
def test_a_close_from_another_thread_ends_a_blocked_pull(tmp_path, closer):
    """A reader waiting on a provider that has gone quiet is released by a close made
    elsewhere — its loop ends, rather than raising a cancellation it never asked for."""
    provider = _Held()
    broker = _broker(tmp_path, provider, "a")
    try:
        stream = broker.stream("hi")
        assert next(stream) == "one"
        with ThreadPoolExecutor(max_workers=1) as pool:
            rest = pool.submit(list, stream)
            assert provider.bodies[0].closed.wait(timeout=0.2) is False  # the reader is blocked
            (stream if closer == "stream" else broker).close()
            assert rest.result(timeout=5.0) == []
        assert provider.bodies[0].closed.is_set()
    finally:
        broker.close()


class _Slow(httpx.AsyncByteStream):
    async def __aiter__(self):
        for d in ("a", "b", "c"):
            yield _delta(d)
            await asyncio.sleep(0.05)
        yield b'data: {"choices": [], "usage": {"total_tokens": 7}}\n\n'
        yield b"data: [DONE]\n\n"


def test_the_wait_budget_pauses_while_a_sync_reader_holds_a_delta(tmp_path):
    """Each delta is held longer than the whole ``wait``: the budget runs only while the
    reader waits on the provider, not while the provider waits on the reader."""
    handler = lambda _request: httpx.Response(200, stream=_Slow(), headers=_SSE_HEADERS)
    with _broker(tmp_path, handler, "a") as broker:
        got: list[str] = []
        for delta in broker.stream("hi", wait=0.4):
            got.append(delta)
            time.sleep(0.5)
    assert got == ["a", "b", "c"]
