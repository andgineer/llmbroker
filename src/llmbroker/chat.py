"""OpenAI-compatible chat primitives. Request building, response parsing and retry
parsing live here once; the resolved key is passed in, never read off the config."""

import json
import math
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from llmbroker.exceptions import InvalidProviderResponseError
from llmbroker.models import LLMConfig, Usage

_CHAT_PATH = "/chat/completions"
HTTP_TIMEOUT = 60.0
_BODY_SNIPPET = 300
_MAX_INT64 = 2**63 - 1


def retry_after_seconds(headers: Mapping[str, str], default_sec: int) -> int:
    """Parse ``Retry-After`` as either delay-seconds or an HTTP-date, per RFC 9110."""
    raw = headers.get("Retry-After")
    if raw is None:
        return default_sec
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return default_sec
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0, int((when - datetime.now(UTC)).total_seconds()))


def build_chat_request(  # noqa: PLR0913
    base_url: str,
    model: str,
    api_key: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    *,
    stream: bool = False,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """Return (url, headers, json_body) for an OpenAI-compatible chat completion.

    >>> url, headers, body = build_chat_request("https://x/v1", "m", "k", [], stream=True)
    >>> url
    'https://x/v1/chat/completions'
    >>> body["stream"], body["stream_options"]
    (True, {'include_usage': True})
    """
    body: dict[str, Any] = {"model": model, "messages": messages}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if stream:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    return (
        f"{base_url}{_CHAT_PATH}",
        {"Authorization": f"Bearer {api_key}"},
        body,
    )


def message_from_response(data: dict) -> dict:
    """Extract the assistant message object from a chat-completion response body."""
    return data["choices"][0]["message"]


def _token_count(value: object) -> int | None:
    """One reported token count, or ``None`` when it is not one the journal can
    store: a non-number, a fraction, or a magnitude past a 64-bit column — keeping
    that last one loses the whole row on insert (``1e400`` decodes to ``inf``).

    >>> _token_count(7), _token_count("12"), _token_count(float("inf")), _token_count(10**30)
    (7, None, None, None)
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (not math.isfinite(value) or value != int(value)):
        return None
    return int(value) if -_MAX_INT64 - 1 <= value <= _MAX_INT64 else None


def parse_usage(data: dict) -> Usage | None:
    """Extract token counts from a chat-completion response body, if present."""
    raw = data.get("usage")
    if not isinstance(raw, dict):
        return None
    known = {"prompt_tokens", "completion_tokens", "total_tokens"}
    extra = {
        k: count
        for k, v in raw.items()
        if k not in known and (count := _token_count(v)) is not None
    }
    return Usage(
        prompt_tokens=_token_count(raw.get("prompt_tokens")),
        completion_tokens=_token_count(raw.get("completion_tokens")),
        total_tokens=_token_count(raw.get("total_tokens")),
        extra=extra or None,
    )


def make_client(timeout: float = HTTP_TIMEOUT) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


@asynccontextmanager
async def _resolve_client(
    client: httpx.AsyncClient | None,
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield the caller's shared client untouched, or an ephemeral one closed on exit."""
    if client is not None:
        yield client
    else:
        async with make_client() as ephemeral:
            yield ephemeral


def _invalid_body(model: str, snippet: str) -> InvalidProviderResponseError:
    return InvalidProviderResponseError(
        f"{model}: HTTP 200 body is not an OpenAI-compatible chat completion",
        model=model,
        detail=snippet[:_BODY_SNIPPET],
    )


def _invalid_stream(model: str, detail: str) -> InvalidProviderResponseError:
    return InvalidProviderResponseError(
        f"{model}: HTTP 200 body is not an OpenAI-compatible SSE stream",
        model=model,
        detail=detail[:_BODY_SNIPPET],
    )


def _parse_completion(data: Any, model: str) -> tuple[str, list[dict] | None, Usage | None]:
    """Pull (content, tool_calls, usage) out of a decoded chat-completion body. The
    caught set is deliberately broad: an escape here reaches the caller raw and skips
    failover."""
    try:
        message = message_from_response(data)
        content = str(message.get("content") or "")
        return content, parse_tool_calls(message), parse_usage(data)
    except (ArithmeticError, AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise _invalid_body(model, str(data)) from exc


def completion_from_response(
    resp: httpx.Response,
    model: str,
) -> tuple[str, list[dict] | None, Usage | None]:
    """Decode a successful chat-completion response and pull its parts out. Anything
    the schema does not admit raises ``InvalidProviderResponseError``, so no caller of
    a 200 sees a raw decoding error."""
    try:
        data = resp.json()
    except ValueError as exc:
        raise _invalid_body(model, resp.text) from exc
    return _parse_completion(data, model)


async def call_provider(  # noqa: PLR0913
    config: LLMConfig,
    api_key: str,
    messages: list[dict],
    tools: list[dict] | None,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float | None = None,
) -> tuple[str, list[dict] | None, Usage | None]:
    """POST an OpenAI-compatible completion and return (content, tool_calls, usage).
    A passed ``client`` is reused and never closed here; ``timeout`` bounds this one
    request, overriding the client's own."""
    url, headers, body = build_chat_request(config.base_url, config.model, api_key, messages, tools)
    async with _resolve_client(client) as active:
        if timeout is None:
            resp = await active.post(url, headers=headers, json=body)
        else:
            resp = await active.post(url, headers=headers, json=body, timeout=timeout)
        resp.raise_for_status()
    return completion_from_response(resp, config.name)


def parse_tool_calls(message: dict) -> list[dict] | None:
    """Extract the raw ``tool_calls`` list from an assistant message, verbatim."""
    tool_calls = message.get("tool_calls")
    if not tool_calls:
        return None
    return tool_calls


_SSE_DATA_PREFIX = "data:"
_SSE_DONE = "[DONE]"


async def aiter_sse_chunks(response: httpx.Response) -> AsyncIterator[dict]:
    """Yield decoded JSON objects from an OpenAI-compatible SSE stream body.

    ``data: [DONE]`` ends the stream; a payload that does not decode, or decodes to
    a scalar or array, is skipped — every consumer here may assume an object.
    """
    async for raw in response.aiter_lines():
        line = raw.strip()
        if not line.startswith(_SSE_DATA_PREFIX):
            continue
        payload = line[len(_SSE_DATA_PREFIX) :].strip()
        if payload == _SSE_DONE:
            return
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            yield decoded


async def aiter_chat_chunks(resp: httpx.Response, model: str) -> AsyncIterator[dict]:
    """Yield the decoded chunks of a chat-completion SSE body. ``choices`` is what
    makes a chunk one, so a body decoding none raises on exhaustion; counting
    *deltas* instead would reject a legitimately empty answer."""
    completions = 0
    async for chunk in aiter_sse_chunks(resp):
        completions += "choices" in chunk
        yield chunk
    if not completions:
        raise _invalid_stream(
            model,
            f"content-type={resp.headers.get('content-type', '')!r},"
            " no chat-completion chunks decoded",
        )


def _stream_delta(chunk: dict) -> str:
    """Text delta carried by one OpenAI-compatible stream chunk (``""`` when none).

    >>> _stream_delta({"choices": [{"delta": {"content": "Hi"}}]})
    'Hi'
    >>> _stream_delta({"choices": [], "usage": {"total_tokens": 3}})
    ''
    """
    choices = chunk.get("choices") or []
    if not choices:
        return ""
    return str(choices[0].get("delta", {}).get("content") or "")


def parse_stream_chunk(chunk: dict, model: str) -> tuple[str, Usage | None]:
    """Pull (delta, usage) out of one stream chunk. The caught set is deliberately
    broad: an escape here reaches the caller raw and skips failover."""
    try:
        return _stream_delta(chunk), parse_usage(chunk)
    except (ArithmeticError, AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise _invalid_stream(model, str(chunk)) from exc
