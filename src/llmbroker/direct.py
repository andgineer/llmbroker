"""Direct single-model client: call one named OpenAI-compatible model directly.

No pool, no failover, no quality-routing, no journal — the streaming-capable
counterpart the pooled ``AsyncBroker`` architecturally cannot be. Reuses the
OpenAI-compatible request/response primitives in ``chat.py`` so nothing here
re-implements request building or response parsing.
"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass

import httpx

from llmbroker.chat import (
    aiter_sse_chunks,
    build_chat_request,
    make_client,
    message_from_response,
    parse_usage,
    retry_after_seconds,
    stream_delta,
)
from llmbroker.exceptions import AuthError, LLMTimeoutError, ProviderError, RateLimitError
from llmbroker.models import Usage

_HTTP_ERROR_FLOOR = 400
_HTTP_401 = 401
_HTTP_403 = 403
_HTTP_429 = 429
_HTTP_503 = 503
_DEFAULT_TIMEOUT = 60.0


@dataclass(frozen=True, slots=True)
class DirectResult:
    """The full, non-streaming reply from a direct call."""

    text: str
    usage: Usage | None = None


def _messages(prompt: str | None, messages: list[dict] | None) -> list[dict]:
    """Normalize the input surface: exactly one of ``prompt`` or ``messages``."""
    if (prompt is None) == (messages is None):
        raise ValueError("pass exactly one of `prompt` or `messages`")
    if messages is not None:
        return messages
    return [{"role": "user", "content": prompt}]


def _provider_error(status: int, detail: str, headers: Mapping[str, str]) -> ProviderError:
    """Map an error HTTP status onto the direct-client error hierarchy."""
    if status in (_HTTP_401, _HTTP_403):
        return AuthError(f"provider rejected the key (HTTP {status})", status=status, detail=detail)
    if status in (_HTTP_429, _HTTP_503):
        retry_after = None
        if headers.get("Retry-After") is not None:
            retry_after = retry_after_seconds(headers, 0)
        return RateLimitError(
            f"provider rate-limited or unavailable (HTTP {status})",
            status=status,
            detail=detail,
            retry_after=retry_after,
        )
    return ProviderError(f"provider returned HTTP {status}", status=status, detail=detail)


def _result(status: int, headers: Mapping[str, str], detail: str, data: dict) -> DirectResult:
    """Build a ``DirectResult`` from a completed response, raising on error status."""
    if status >= _HTTP_ERROR_FLOOR:
        raise _provider_error(status, detail, headers)
    message = message_from_response(data)
    return DirectResult(text=str(message.get("content") or ""), usage=parse_usage(data))


class AsyncDirectClient:
    """Async direct client for one named model — ``stream()`` and ``ask()``.

    Single model, no pool, no failover. Pass an ``httpx.AsyncClient`` to reuse a
    shared connection pool; otherwise one is opened lazily and closed by
    ``aclose()`` (or the async context manager).
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout: float = _DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url
        self._model = model
        self._api_key = api_key
        self._timeout = timeout
        self._http = client
        self._owns_http = client is None

    def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = make_client(self._timeout)
        return self._http

    def _request(
        self,
        prompt: str | None,
        messages: list[dict] | None,
        *,
        stream: bool = False,
    ) -> tuple[str, dict[str, str], dict]:
        return build_chat_request(
            self._base_url,
            self._model,
            self._api_key,
            _messages(prompt, messages),
            stream=stream,
        )

    async def ask(
        self,
        prompt: str | None = None,
        *,
        messages: list[dict] | None = None,
        timeout: float | None = None,
    ) -> DirectResult:
        url, headers, body = self._request(prompt, messages)
        try:
            resp = await self._ensure_http().post(
                url,
                headers=headers,
                json=body,
                timeout=timeout or self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("direct call timed out") from exc
        return _result(resp.status_code, resp.headers, resp.text[:300], _safe_json(resp))

    async def stream(
        self,
        prompt: str | None = None,
        *,
        messages: list[dict] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[str]:
        url, headers, body = self._request(prompt, messages, stream=True)
        try:
            async with self._ensure_http().stream(
                "POST",
                url,
                headers=headers,
                json=body,
                timeout=timeout or self._timeout,
            ) as resp:
                if resp.status_code >= _HTTP_ERROR_FLOOR:
                    detail = (await resp.aread()).decode(errors="replace")[:300]
                    raise _provider_error(resp.status_code, detail, resp.headers)
                async for chunk in aiter_sse_chunks(resp):
                    delta = stream_delta(chunk)
                    if delta:
                        yield delta
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
    """Synchronous direct client for one named model — ``ask()`` only.

    Streaming is async-only; this blocking client is a single ``POST`` and needs
    no event loop. Pass an ``httpx.Client`` to reuse a shared connection pool;
    otherwise one is opened lazily and closed by ``close()`` (or the context
    manager).
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout: float = _DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url
        self._model = model
        self._api_key = api_key
        self._timeout = timeout
        self._http = client
        self._owns_http = client is None

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
    ) -> DirectResult:
        url, headers, body = build_chat_request(
            self._base_url,
            self._model,
            self._api_key,
            _messages(prompt, messages),
        )
        try:
            resp = self._ensure_http().post(
                url,
                headers=headers,
                json=body,
                timeout=timeout or self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("direct call timed out") from exc
        return _result(resp.status_code, resp.headers, resp.text[:300], _safe_json(resp))

    def close(self) -> None:
        if self._owns_http and self._http is not None:
            self._http.close()
            self._http = None

    def __enter__(self) -> "DirectClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _safe_json(resp: httpx.Response) -> dict:
    """Decode a JSON body, or ``{}`` on an error response that carried none."""
    if resp.status_code >= _HTTP_ERROR_FLOOR:
        try:
            return resp.json()
        except ValueError:
            return {}
    return resp.json()


__all__ = [
    "AsyncDirectClient",
    "DirectClient",
    "DirectResult",
]
