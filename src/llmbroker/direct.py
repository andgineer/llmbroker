"""Direct single-model client: no pool, no failover, no journal. Reuses the
request/response primitives in ``chat.py``."""

from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from functools import partial

import httpx

from llmbroker.chat import (
    NO_DELTA,
    aiter_chat_chunks,
    build_chat_request,
    completion_from_response,
    empty_answer_error,
    iter_chat_chunks,
    make_client,
    parse_stream_chunk,
    provider_error,
)
from llmbroker.exceptions import LLMTimeoutError
from llmbroker.http_status import DETAIL_SNIPPET, ERROR_FLOOR
from llmbroker.models import Usage

_DEFAULT_TIMEOUT = 60.0


@dataclass(frozen=True, slots=True)
class DirectResult:
    """The full, non-streaming reply from a direct call."""

    text: str
    usage: Usage | None = None
    tool_calls: list[dict] | None = None


def _messages(prompt: str | None, messages: list[dict] | None) -> list[dict]:
    """Normalize the input surface: exactly one of ``prompt`` or ``messages``."""
    if (prompt is None) == (messages is None):
        raise ValueError("pass exactly one of `prompt` or `messages`")
    if messages is not None:
        return messages
    return [{"role": "user", "content": prompt}]


def _result(resp: httpx.Response, model: str) -> DirectResult:
    """Build a ``DirectResult`` from a completed response, raising on an error status
    and on a 200 that carried no answer."""
    if resp.status_code >= ERROR_FLOOR:
        raise provider_error(resp.status_code, resp.text[:DETAIL_SNIPPET], resp.headers)
    text, tool_calls, usage = completion_from_response(resp, model)
    return DirectResult(text=text, usage=usage, tool_calls=tool_calls)


class AsyncDirectClient:
    """Async direct client for one named model — ``stream()``, ``ask()`` and ``chat()``.
    Pass an ``httpx.AsyncClient`` to share a connection pool, or let it open and close
    its own; ``tool_params`` ride every ``chat`` that carries tools, under ``params``."""

    def __init__(  # noqa: PLR0913 - where the model is, and how to reach it
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout: float = _DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
        tool_params: Mapping[str, object] | None = None,
    ) -> None:
        self._model = model
        self._timeout = timeout
        self._http = client
        self._owns_http = client is None
        self._request = partial(
            build_chat_request,
            base_url,
            model,
            api_key,
            tool_params=tool_params,
        )

    def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = make_client(self._timeout)
        return self._http

    async def ask(
        self,
        prompt: str | None = None,
        *,
        messages: list[dict] | None = None,
        timeout: float | None = None,
        params: Mapping[str, object] | None = None,
    ) -> DirectResult:
        return await self.chat(_messages(prompt, messages), timeout=timeout, params=params)

    async def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        timeout: float | None = None,
        params: Mapping[str, object] | None = None,
    ) -> DirectResult:
        """One complete reply, which may be tool calls and no text."""
        url, headers, body = self._request(messages, tools, params=params)
        try:
            resp = await self._ensure_http().post(
                url,
                headers=headers,
                json=body,
                timeout=timeout or self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("direct call timed out") from exc
        return _result(resp, self._model)

    async def stream(
        self,
        prompt: str | None = None,
        *,
        messages: list[dict] | None = None,
        timeout: float | None = None,
        params: Mapping[str, object] | None = None,
    ) -> AsyncIterator[str]:
        url, headers, body = self._request(
            _messages(prompt, messages),
            stream=True,
            params=params,
        )
        try:
            async with self._ensure_http().stream(
                "POST",
                url,
                headers=headers,
                json=body,
                timeout=timeout or self._timeout,
            ) as resp:
                if resp.status_code >= ERROR_FLOOR:
                    detail = (await resp.aread()).decode(errors="replace")[:DETAIL_SNIPPET]
                    raise provider_error(resp.status_code, detail, resp.headers)
                produced = False
                async for chunk in aiter_chat_chunks(resp, self._model):
                    delta, _ = parse_stream_chunk(chunk, self._model)
                    if delta:
                        produced = True
                        yield delta
                if not produced:
                    raise empty_answer_error(self._model, NO_DELTA)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("direct stream timed out") from exc

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> "AsyncDirectClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


class DirectClient:
    """Synchronous direct client for one named model — ``stream()``, ``ask()`` and
    ``chat()``, none needing an event loop. ``client`` and ``tool_params`` mean what
    they do on ``AsyncDirectClient``."""

    def __init__(  # noqa: PLR0913 - where the model is, and how to reach it
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout: float = _DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
        tool_params: Mapping[str, object] | None = None,
    ) -> None:
        self._model = model
        self._timeout = timeout
        self._http = client
        self._owns_http = client is None
        self._request = partial(
            build_chat_request,
            base_url,
            model,
            api_key,
            tool_params=tool_params,
        )

    def _ensure_http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self._timeout)
        return self._http

    def ask(
        self,
        prompt: str | None = None,
        *,
        messages: list[dict] | None = None,
        timeout: float | None = None,
        params: Mapping[str, object] | None = None,
    ) -> DirectResult:
        return self.chat(_messages(prompt, messages), timeout=timeout, params=params)

    def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        timeout: float | None = None,
        params: Mapping[str, object] | None = None,
    ) -> DirectResult:
        """One complete reply, which may be tool calls and no text."""
        url, headers, body = self._request(messages, tools, params=params)
        try:
            resp = self._ensure_http().post(
                url,
                headers=headers,
                json=body,
                timeout=timeout or self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("direct call timed out") from exc
        return _result(resp, self._model)

    def stream(
        self,
        prompt: str | None = None,
        *,
        messages: list[dict] | None = None,
        timeout: float | None = None,
        params: Mapping[str, object] | None = None,
    ) -> Iterator[str]:
        """Text deltas as they arrive. Closing the iterator early closes the response,
        and with it the connection the provider is writing to."""
        url, headers, body = self._request(
            _messages(prompt, messages),
            stream=True,
            params=params,
        )
        try:
            with self._ensure_http().stream(
                "POST",
                url,
                headers=headers,
                json=body,
                timeout=timeout or self._timeout,
            ) as resp:
                if resp.status_code >= ERROR_FLOOR:
                    detail = resp.read().decode(errors="replace")[:DETAIL_SNIPPET]
                    raise provider_error(resp.status_code, detail, resp.headers)
                produced = False
                for chunk in iter_chat_chunks(resp, self._model):
                    delta, _ = parse_stream_chunk(chunk, self._model)
                    if delta:
                        produced = True
                        yield delta
                if not produced:
                    raise empty_answer_error(self._model, NO_DELTA)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("direct stream timed out") from exc

    def close(self) -> None:
        if self._owns_http and self._http is not None:
            self._http.close()
            self._http = None

    def __enter__(self) -> "DirectClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = [
    "AsyncDirectClient",
    "DirectClient",
    "DirectResult",
]
