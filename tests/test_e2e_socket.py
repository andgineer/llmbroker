"""End-to-end over a real socket: no patching anywhere. In-process HTTP servers speak
OpenAI-compatible JSON and SSE — one rate-limits with ``Retry-After: 1``, one answers,
one holds a stream open to see a client hang up — and llmbroker's own httpx clients
talk to them.
"""

import asyncio
import gc
import json
import socket
import threading
import warnings

from llmbroker.broker.broker import AsyncBroker
from llmbroker.direct import DirectClient
from llmbroker.models import LifecyclePhase
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import InMemoryStore
from llmbroker.sync import Broker

_COMPLETION = {"choices": [{"message": {"role": "assistant", "content": "answered"}}]}


class _Server:
    """A minimal HTTP/1.1 endpoint on a loopback port, driven by ``responder``."""

    def __init__(self, responder) -> None:
        self._responder = responder
        self._server: asyncio.Server | None = None
        self.requests = 0

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = _content_length(head)
                if length:
                    await reader.readexactly(length)
                self.requests += 1
                writer.write(self._responder(self.requests))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()


def _content_length(head: bytes) -> int:
    for line in head.decode("latin-1").split("\r\n"):
        name, _, value = line.partition(":")
        if name.lower() == "content-length":
            return int(value.strip())
    return 0


def _response(status: int, reason: str, body: dict, extra_headers: str = "") -> bytes:
    payload = json.dumps(body).encode()
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"{extra_headers}\r\n"
    ).encode()
    return head + payload


def _rate_limited_once(nth: int) -> bytes:
    """429 with ``Retry-After: 1`` first, then a normal completion."""
    if nth == 1:
        return _response(429, "Too Many Requests", {"error": "slow down"}, "Retry-After: 1\r\n")
    return _response(200, "OK", _COMPLETION)


def _always_ok(_nth: int) -> bytes:
    return _response(200, "OK", _COMPLETION)


def _write_registry(tmp_path, port_a: int, port_b: int):
    f = tmp_path / "llms.toml"
    f.write_text(
        f'[[llms]]\nname="a"\nbase_url="http://127.0.0.1:{port_a}/v1"\n'
        'model="m"\napi_key_ref="K"\n'
        f'[[llms]]\nname="b"\nbase_url="http://127.0.0.1:{port_b}/v1"\n'
        'model="m"\napi_key_ref="K"\n',
    )
    return Registry(f)


async def test_rate_limited_model_fails_over_then_returns_after_its_cooldown(tmp_path):
    server_a, server_b = _Server(_rate_limited_once), _Server(_always_ok)
    await server_a.start()
    await server_b.start()
    try:
        broker = AsyncBroker(
            registry=_write_registry(tmp_path, server_a.port, server_b.port),
            secrets=DictSecrets({"K": "test"}),
            store=InMemoryStore(),
            sync=None,
        )
        async with broker:
            first = await broker.ask("hi")
            assert first.text == "answered"
            assert first.llm_name == "b"  # a is cooling on its own Retry-After
            assert (await (await broker.get("a")).state()).phase is LifecyclePhase.COOLING

            await asyncio.sleep(1.1)  # the provider's own Retry-After, not a tuning knob

            second = await broker.ask("hi")
            assert second.text == "answered"
            assert second.llm_name == "a"  # curated order puts a first again
        assert server_a.requests == 2
    finally:
        await server_a.stop()
        await server_b.stop()


class _HeldStream:
    """A loopback provider that sends one SSE delta, holds the response open, and
    records the moment the client hangs up on it."""

    def __init__(self) -> None:
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self.hung_up = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._listener.close()
        self._thread.join(timeout=5.0)

    def _serve(self) -> None:
        conn, _ = self._listener.accept()
        with conn:
            conn.settimeout(10.0)
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(65536)
            head, _, body = request.partition(b"\r\n\r\n")
            while len(body) < _content_length(head):
                body += conn.recv(65536)
            event = b'data: {"choices": [{"delta": {"content": "one"}}]}\n\n'
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n" + b"%x\r\n%s\r\n" % (len(event), event),
            )
            try:
                while conn.recv(1):
                    pass
            except ConnectionResetError:
                pass
            self.hung_up.set()


def test_closing_a_sync_pool_stream_hangs_up_on_the_provider(tmp_path):
    """The WSGI abort end to end: what closing the sync iterator reaches is the
    provider's own socket, not only an object in this process — and closing the broker
    afterwards leaves nothing of that response for a dead loop to finalize."""
    provider = _HeldStream()
    f = tmp_path / "llms.toml"
    f.write_text(
        f'[[llms]]\nname="a"\nbase_url="http://127.0.0.1:{provider.port}/v1"\n'
        'model="m"\napi_key_ref="K"\n',
    )
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with Broker(
                registry=Registry(f),
                secrets=DictSecrets({"K": "test"}),
                store=InMemoryStore(),
                sync=None,
            ) as broker:
                stream = broker.stream("hi")
                assert next(stream) == "one"
                assert not provider.hung_up.is_set()
                stream.close()
                assert provider.hung_up.wait(timeout=5.0)
            gc.collect()
    finally:
        provider.close()
    assert [str(w.message) for w in caught if "never awaited" in str(w.message)] == []


def test_closing_a_sync_direct_stream_hangs_up_on_the_provider():
    provider = _HeldStream()
    try:
        with DirectClient(
            base_url=f"http://127.0.0.1:{provider.port}/v1",
            model="m",
            api_key="test",
        ) as client:
            deltas = client.stream("hi")
            assert next(deltas) == "one"
            assert not provider.hung_up.is_set()
            deltas.close()
            assert provider.hung_up.wait(timeout=5.0)
    finally:
        provider.close()
