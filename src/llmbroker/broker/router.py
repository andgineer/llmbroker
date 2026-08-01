"""Router: route one completion over the pool with per-LLM failover.

Acquires a free slot, calls the provider, and journals every attempt; each
failure's disposal — cool down and retry, fail over without cooling, or hand the
caller back its own expired ``wait`` — is the error contract in architecture.md.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn

import httpx

from llmbroker import chat
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.result import AsyncResult
from llmbroker.chat import (
    aiter_sse_chunks,
    build_chat_request,
    call_provider,
    is_rate_limit,
    parse_usage,
    retry_after_seconds,
    stream_delta,
)
from llmbroker.exceptions import (
    InvalidProviderResponseError,
    NoLLMAvailableError,
    StreamInterruptedError,
)
from llmbroker.models import Call, CallStatus, LLMConfig, Usage, key_hash
from llmbroker.optimizer import Optimizer
from llmbroker.protocols.store import StoreProtocol

HTTP_429 = 429
HTTP_401 = 401
HTTP_403 = 403
_HTTP_ERROR_FLOOR = 400
_DEFAULT_RATE_LIMIT_SEC = 60
_DETAIL_SNIPPET = 300

logger = logging.getLogger("llmbroker.broker")


def _is_client_error(code: int) -> bool:
    return 400 <= code < 500 and code not in (HTTP_429, HTTP_401, HTTP_403)  # noqa: PLR2004


@dataclass(frozen=True)
class _Failed:
    """One candidate is done for this request; ``error`` is set only for a
    genuine client error (never for a dead key), so the router can decide
    whether to surface it once every candidate is exhausted."""

    error: httpx.HTTPStatusError | None


@dataclass(frozen=True)
class _BudgetExpired:
    """The caller's own ``wait`` ran out mid-attempt — nobody's fault but the
    clock's, so the model is neither cooled nor counted as failing."""


@dataclass(frozen=True)
class _Verdict:
    """How one failed attempt is disposed of and journaled. A ``cool_base`` of
    ``None`` means release the slot instead of cooling the model down."""

    status: CallStatus
    detail: str | None
    http_status: int | None = None
    cool_base: float | None = None
    outcome: "_Failed | _BudgetExpired | None" = None


@dataclass(frozen=True, slots=True)
class _Attempt:
    """Identity of one in-flight attempt — what its journal row is keyed by."""

    config: LLMConfig
    call_id: str
    t0: float
    operation: str | None
    trace_id: str | None
    resolved_key: str


@dataclass(slots=True)
class _StreamProgress:
    """What one streaming attempt has produced so far — what its failure handling
    and its journal row are decided by, once the generator has been unwound."""

    started: bool = False
    usage: Usage | None = None


async def _stream_deltas(
    client: httpx.AsyncClient,
    request: tuple[str, dict[str, str], dict],
    *,
    model: str,
    timeout: float,
    progress: _StreamProgress,
) -> AsyncIterator[str]:
    """Open one streaming request and yield its text deltas, recording progress.

    ``timeout`` bounds the wait for the first delta only: the client's own
    per-operation ceiling bounds every read after it, so a slow *consumer* — which
    suspends this generator between deltas — can never trip a deadline.
    """
    url, headers, body = request
    async with (
        asyncio.timeout(timeout) as bound,
        client.stream("POST", url, headers=headers, json=body) as resp,
    ):
        if resp.status_code >= _HTTP_ERROR_FLOOR:
            await resp.aread()
            resp.raise_for_status()
        completions = 0
        async for chunk in aiter_sse_chunks(resp):
            # `choices` is what makes a chunk a chat completion. Counting decoded
            # chunks instead would accept a 200 whose whole body is an SSE-framed
            # provider error, and counting *deltas* would reject a legitimately
            # empty answer.
            completions += "choices" in chunk
            progress.usage = parse_usage(chunk) or progress.usage
            delta = stream_delta(chunk)
            if not delta:
                continue
            if not progress.started:
                progress.started = True
                bound.reschedule(None)
            yield delta
        if not completions:
            # A proxy's error page, a plain JSON body, a provider ignoring `stream`,
            # an SSE-framed error payload. Same verdict as a garbage 200 on the
            # non-streaming path: cool down and fail over.
            raise InvalidProviderResponseError(
                f"{model}: HTTP 200 body is not an OpenAI-compatible SSE stream",
                model=model,
                detail=f"content-type={resp.headers.get('content-type', '')!r},"
                " no chat-completion chunks decoded",
            )


class _StreamRetry(Exception):  # noqa: N818 - internal control flow, never reaches a caller
    """A streaming attempt died before its first delta; the slot is already
    disposed of and the attempt journaled, so only the next candidate is left to
    decide. ``outcome`` is the same verdict a ``chat`` attempt would have returned."""

    def __init__(self, outcome: "_Failed | _BudgetExpired | None") -> None:
        super().__init__("stream attempt failed before its first delta")
        self.outcome = outcome


_FAILOVER_ERRORS = (
    httpx.HTTPStatusError,
    httpx.TransportError,
    InvalidProviderResponseError,
    OSError,
)


def _classify_status(exc: httpx.HTTPStatusError) -> _Verdict:
    code = exc.response.status_code
    detail = exc.response.text[:_DETAIL_SNIPPET]
    if is_rate_limit(code):
        status = CallStatus.RATE_LIMITED if code == HTTP_429 else CallStatus.UNAVAILABLE
        base = retry_after_seconds(exc.response.headers, _DEFAULT_RATE_LIMIT_SEC)
        return _Verdict(status, detail, http_status=code, cool_base=base)
    if code in (HTTP_401, HTTP_403):
        return _Verdict(
            CallStatus.ERROR,
            detail,
            http_status=code,
            cool_base=_DEFAULT_RATE_LIMIT_SEC,
            outcome=_Failed(error=None),
        )
    if _is_client_error(code):
        return _Verdict(CallStatus.ERROR, detail, http_status=code, outcome=_Failed(error=exc))
    return _Verdict(CallStatus.ERROR, detail, http_status=code, cool_base=_DEFAULT_RATE_LIMIT_SEC)


def _classify(exc: Exception, *, budget_bound: bool) -> _Verdict:
    """Map a failed attempt onto its disposal: cool down and retry the next LLM,
    fail over without cooling, or hand the caller its own expired ``wait`` back."""
    if isinstance(exc, httpx.HTTPStatusError):
        return _classify_status(exc)
    if isinstance(exc, InvalidProviderResponseError):
        return _Verdict(CallStatus.ERROR, exc.detail, cool_base=_DEFAULT_RATE_LIMIT_SEC)
    if budget_bound and isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return _Verdict(
            CallStatus.ERROR,
            f"wait budget exhausted: {type(exc).__name__}",
            outcome=_BudgetExpired(),
        )
    return _Verdict(CallStatus.ERROR, type(exc).__name__, cool_base=_DEFAULT_RATE_LIMIT_SEC)


class Router:
    """Routes a completion request over the pool, failing over between LLMs."""

    def __init__(
        self,
        pool: LLMPool,
        store: StoreProtocol,
        *,
        scope: str | None,
        optimizer: Optimizer | None = None,
    ) -> None:
        self._pool = pool
        self._store = store
        self._scope = scope
        self._optimizer = optimizer
        self._http: httpx.AsyncClient | None = None

    async def ask(
        self,
        prompt: str,
        *,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
    ) -> AsyncResult:
        return await self.chat(
            [{"role": "user", "content": prompt}],
            operation=operation,
            trace_id=trace_id,
            wait=wait,
        )

    async def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
    ) -> AsyncResult:
        queue_deadline = None if wait is None else time.monotonic() + wait
        # wait=0 is "do not queue", not "answer instantly": it bounds slot
        # acquisition only, leaving the attempt on the global ceiling.
        answer_deadline = queue_deadline if wait else None
        client_failed: set[str] = set()
        last_client_error: httpx.HTTPStatusError | None = None
        while True:
            try:
                config = await self._pool.acquire(
                    queue_deadline,
                    operation=operation,
                    exclude=frozenset(client_failed),
                    answer_deadline=answer_deadline,
                )
            except NoLLMAvailableError as exc:
                if exc.reason == "excluded" and last_client_error is not None:
                    raise last_client_error from None
                raise

            outcome = await self._attempt(
                config,
                messages,
                tools,
                operation=operation,
                trace_id=trace_id,
                answer_deadline=answer_deadline,
            )
            if isinstance(outcome, AsyncResult):
                return outcome
            if isinstance(outcome, _BudgetExpired):
                # A request an earlier LLM already rejected as malformed stays the
                # more useful answer than "the clock ran out" — the caller can act on it.
                if last_client_error is not None:
                    raise last_client_error from None
                raise NoLLMAvailableError(
                    "the wait budget ran out while an LLM was answering",
                    reason="timeout",
                )
            if isinstance(outcome, _Failed):
                client_failed.add(config.name)
                if outcome.error is not None:
                    last_client_error = outcome.error
            # None or _Failed with no error ⇒ loop to the next free LLM.

    def _capped_wait(self, base: float, backoff: float) -> float:
        cap = self._optimizer.max_delay if self._optimizer else base
        return min(base * backoff, cap)

    def _attempt_timeout(self, answer_deadline: float | None) -> tuple[float, bool]:
        """Per-attempt HTTP timeout, and whether the caller's remaining ``wait``
        budget — rather than the global ceiling — is what bounds it."""
        if answer_deadline is None:
            return chat.HTTP_TIMEOUT, False
        remaining = answer_deadline - time.monotonic()
        if remaining >= chat.HTTP_TIMEOUT:
            return chat.HTTP_TIMEOUT, False
        return max(remaining, 0.0), True

    def _new_attempt(
        self,
        config: LLMConfig,
        *,
        operation: str | None,
        trace_id: str | None,
    ) -> _Attempt:
        return _Attempt(
            config=config,
            call_id=str(uuid.uuid4()),
            t0=time.monotonic(),
            operation=operation,
            trace_id=trace_id,
            resolved_key=self._pool.resolved_key(config.name),
        )

    def _backoff(self, name: str) -> float:
        # Read before the first record is awaited (which increments rl_fail_count via
        # the learning hook), so the first failure in a streak always sees exponent 0.
        fails_before = self._optimizer.rl_fail_count(name) if self._optimizer else 0
        return self._optimizer.backoff_factor**fails_before if self._optimizer else 1.0

    async def _record(  # noqa: PLR0913
        self,
        attempt: _Attempt,
        status: CallStatus,
        *,
        http_status: int | None = None,
        error_detail: str | None = None,
        usage: Usage | None = None,
        cooldown_delay: float | None = None,
    ) -> None:
        cooldown_until = (
            datetime.now(UTC) + timedelta(seconds=cooldown_delay)
            if cooldown_delay is not None
            else None
        )
        await self._log_call(
            Call(
                id=attempt.call_id,
                llm_name=attempt.config.name,
                operation=attempt.operation,
                trace_id=attempt.trace_id,
                status=status,
                ts=datetime.now(UTC),
                http_status=http_status,
                latency_ms=int((time.monotonic() - attempt.t0) * 1000),
                error_detail=error_detail,
                usage=usage,
                scope=self._scope,
                cooldown_until=cooldown_until,
                key_hash=key_hash(attempt.resolved_key) if cooldown_delay is not None else None,
            ),
        )

    async def _finish_ok(self, attempt: _Attempt, usage: Usage | None) -> None:
        await self._pool.release(attempt.config)
        self._pool.clear_cooling(attempt.config.name)
        self._pool.clear_unmet_budget(attempt.config.name)
        await self._record(attempt, CallStatus.OK, http_status=200, usage=usage)

    async def _dispose(
        self,
        attempt: _Attempt,
        verdict: _Verdict,
        *,
        backoff: float,
        timeout: float,
    ) -> None:
        """Settle one failed attempt: cool the model down or just hand the slot
        back, then journal it. The single failure surface both routing paths use."""
        delay: float | None = None
        if verdict.cool_base is None:
            await self._pool.release(attempt.config)
            if isinstance(verdict.outcome, _BudgetExpired):
                # Not a penalty: the only latency this model will ever report is the
                # budget it just failed to meet, and the next caller offering no more
                # than that should be handed a sibling first.
                self._pool.note_unmet_budget(attempt.config, timeout)
        else:
            delay = self._capped_wait(verdict.cool_base, backoff)
            await self._pool.cool_down(attempt.config, delay)
        await self._record(
            attempt,
            verdict.status,
            http_status=verdict.http_status,
            error_detail=verdict.detail,
            cooldown_delay=delay,
        )

    async def _attempt(  # noqa: PLR0913
        self,
        config: LLMConfig,
        messages: list[dict],
        tools: list[dict] | None,
        *,
        operation: str | None,
        trace_id: str | None,
        answer_deadline: float | None = None,
    ) -> AsyncResult | _Failed | _BudgetExpired | None:
        """Run one LLM. Return its result, ``None`` to try the next LLM after a
        cooldown, ``_Failed`` to try the next LLM without cooling this one, or
        ``_BudgetExpired`` when the caller's own ``wait`` ran out."""
        attempt = self._new_attempt(config, operation=operation, trace_id=trace_id)
        backoff = self._backoff(config.name)

        if self._http is None:
            self._http = chat.make_client()

        timeout, budget_bound = self._attempt_timeout(answer_deadline)
        if budget_bound and timeout == 0.0:
            await self._pool.release(config)
            await self._record(attempt, CallStatus.ERROR, error_detail="wait budget exhausted")
            return _BudgetExpired()

        try:
            # httpx applies its timeout per operation (connect, write, read), so
            # only this wall-clock bound keeps the whole attempt inside the budget.
            async with asyncio.timeout(timeout):
                content, tool_calls, usage = await call_provider(
                    config,
                    attempt.resolved_key,
                    messages,
                    tools,
                    client=self._http,
                    timeout=timeout,
                )
        except _FAILOVER_ERRORS as exc:
            verdict = _classify(exc, budget_bound=budget_bound)
        except BaseException as exc:
            # A bug, or a caller that cancelled us: surface it, but never leak the
            # slot — a cancelled call must not cost the model a unit of `parallel`.
            await self._pool.release(config)
            if isinstance(exc, Exception):
                await self._record(attempt, CallStatus.ERROR, error_detail=type(exc).__name__)
            raise
        else:
            await self._finish_ok(attempt, usage)
            return AsyncResult(
                text=content,
                tool_calls=tool_calls,
                usage=usage,
                call_id=attempt.call_id,
                llm_name=config.name,
                operation=operation,
                store=self._store,
                scope=self._scope,
            )

        await self._dispose(attempt, verdict, backoff=backoff, timeout=timeout)
        return verdict.outcome

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def stream(
        self,
        messages: list[dict],
        *,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
    ) -> AsyncIterator[str]:
        """Route a streaming completion over the pool, yielding text deltas.

        Fails over exactly like ``chat`` up to the first delta; past it the
        answer is already the caller's and a death raises ``StreamInterruptedError``.
        """
        queue_deadline = None if wait is None else time.monotonic() + wait
        # `wait` bounds slot acquisition and time to first delta — the stretch a
        # failover can still rescue. Past it the pace is the consumer's as much as
        # the model's, and only the global per-read ceiling applies.
        first_delta_deadline = queue_deadline if wait else None
        client_failed: set[str] = set()
        last_client_error: httpx.HTTPStatusError | None = None
        while True:
            try:
                config = await self._pool.acquire(
                    queue_deadline,
                    operation=operation,
                    exclude=frozenset(client_failed),
                    answer_deadline=first_delta_deadline,
                )
            except NoLLMAvailableError as exc:
                if exc.reason == "excluded" and last_client_error is not None:
                    raise last_client_error from None
                raise

            try:
                async with aclosing(
                    self._stream_attempt(
                        config,
                        messages,
                        operation=operation,
                        trace_id=trace_id,
                        first_delta_deadline=first_delta_deadline,
                    ),
                ) as deltas:
                    async for delta in deltas:
                        yield delta
            except _StreamRetry as retry:
                if isinstance(retry.outcome, _BudgetExpired):
                    if last_client_error is not None:
                        raise last_client_error from None
                    raise NoLLMAvailableError(
                        "the wait budget ran out before any LLM produced a delta",
                        reason="timeout",
                    ) from None
                if isinstance(retry.outcome, _Failed):
                    client_failed.add(config.name)
                    if retry.outcome.error is not None:
                        last_client_error = retry.outcome.error
                continue
            return

    async def _stream_attempt(
        self,
        config: LLMConfig,
        messages: list[dict],
        *,
        operation: str | None,
        trace_id: str | None,
        first_delta_deadline: float | None,
    ) -> AsyncIterator[str]:
        """Stream one LLM, yielding its deltas.

        Raises ``_StreamRetry`` when it died before the first delta — the slot is
        settled and journaled by then, so the caller only picks the next candidate.
        """
        attempt = self._new_attempt(config, operation=operation, trace_id=trace_id)
        backoff = self._backoff(config.name)
        if self._http is None:
            self._http = chat.make_client()

        timeout, budget_bound = self._attempt_timeout(first_delta_deadline)
        if budget_bound and timeout == 0.0:
            await self._pool.release(config)
            await self._record(attempt, CallStatus.ERROR, error_detail="wait budget exhausted")
            raise _StreamRetry(_BudgetExpired())

        request = build_chat_request(
            config.base_url,
            config.model,
            attempt.resolved_key,
            messages,
            stream=True,
        )
        progress = _StreamProgress()
        try:
            async with aclosing(
                _stream_deltas(
                    self._http,
                    request,
                    model=config.name,
                    timeout=timeout,
                    progress=progress,
                ),
            ) as deltas:
                async for delta in deltas:
                    yield delta
        except _FAILOVER_ERRORS as exc:
            await self._fail_stream(
                attempt,
                exc,
                backoff=backoff,
                timeout=timeout,
                budget_bound=budget_bound,
                started=progress.started,
            )
        except GeneratorExit:
            # The consumer stopped pulling. The model answered and did nothing wrong,
            # so this is a completed attempt, not a failure the pool should learn from.
            await self._finish_ok(attempt, progress.usage)
            raise
        except BaseException as exc:
            await self._pool.release(config)
            if isinstance(exc, Exception):
                await self._record(attempt, CallStatus.ERROR, error_detail=type(exc).__name__)
            raise
        else:
            await self._finish_ok(attempt, progress.usage)

    async def _fail_stream(  # noqa: PLR0913
        self,
        attempt: _Attempt,
        exc: Exception,
        *,
        backoff: float,
        timeout: float,
        budget_bound: bool,
        started: bool,
    ) -> NoReturn:
        """Settle a failed streaming attempt through the shared failure surface and
        raise: ``_StreamRetry`` while failover is still possible, and once deltas have
        reached the caller ``StreamInterruptedError``, which nothing can rescue."""
        verdict = _classify(exc, budget_bound=budget_bound and not started)
        await self._dispose(attempt, verdict, backoff=backoff, timeout=timeout)
        if started:
            raise StreamInterruptedError(
                f"{attempt.config.name}: the stream died after it had already emitted"
                " deltas — no failover is possible once output has reached the caller",
                llm_name=attempt.config.name,
            ) from exc
        raise _StreamRetry(verdict.outcome) from None

    async def _log_call(self, call: Call) -> None:
        try:
            await self._store.record(call)
        except Exception:  # noqa: BLE001
            logger.exception("llmbroker: store.record failed")

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
