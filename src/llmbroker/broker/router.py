"""Router: route one completion over the pool with per-LLM failover.

Acquires a free slot, calls the provider, and journals every attempt; each
failure's disposal — cool down and retry, fail over without cooling, or hand the
caller back its own expired ``wait`` — is the error contract in call-path.md.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TypeVar

import httpx

from llmbroker import chat
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.result import AsyncResult
from llmbroker.chat import (
    aiter_chat_chunks,
    build_chat_request,
    call_provider,
    parse_usage,
    retry_after_seconds,
    stream_delta,
)
from llmbroker.exceptions import (
    InvalidProviderResponseError,
    NoLLMAvailableError,
    StreamInterruptedError,
)
from llmbroker.http_status import (
    DETAIL_SNIPPET,
    ERROR_FLOOR,
    is_auth_failure,
    is_client_error,
    is_rate_limit,
    is_unavailable,
)
from llmbroker.models import Call, CallStatus, LLMConfig, Usage, key_hash
from llmbroker.optimizer import Optimizer
from llmbroker.protocols.store import StoreProtocol

_DEFAULT_RATE_LIMIT_SEC = 60

logger = logging.getLogger("llmbroker.broker")

_Produced = TypeVar("_Produced")


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
class _Outcome:
    """What one attempt reports back to the failover driver: the LLM answered, or
    a verdict saying how the next candidate is chosen. The attempt has already
    settled its own slot and journaled its own row by the time either is set."""

    answered: bool = False
    verdict: "_Failed | _BudgetExpired | None" = None


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
        if resp.status_code >= ERROR_FLOOR:
            await resp.aread()
            resp.raise_for_status()
        async for chunk in aiter_chat_chunks(resp, model):
            progress.usage = parse_usage(chunk) or progress.usage
            delta = stream_delta(chunk)
            if not delta:
                continue
            if not progress.started:
                progress.started = True
                bound.reschedule(None)
            yield delta


_FAILOVER_ERRORS = (
    httpx.HTTPStatusError,
    httpx.TransportError,
    InvalidProviderResponseError,
    OSError,
)


def _classify_status(exc: httpx.HTTPStatusError) -> _Verdict:
    code = exc.response.status_code
    detail = exc.response.text[:DETAIL_SNIPPET]
    if is_rate_limit(code):
        status = CallStatus.UNAVAILABLE if is_unavailable(code) else CallStatus.RATE_LIMITED
        base = retry_after_seconds(exc.response.headers, _DEFAULT_RATE_LIMIT_SEC)
        return _Verdict(status, detail, http_status=code, cool_base=base)
    if is_auth_failure(code):
        return _Verdict(
            CallStatus.ERROR,
            detail,
            http_status=code,
            cool_base=_DEFAULT_RATE_LIMIT_SEC,
            outcome=_Failed(error=None),
        )
    if is_client_error(code):
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
        routed = self._route(
            partial(self._attempt, messages=messages, tools=tools),
            operation=operation,
            trace_id=trace_id,
            wait=wait,
            timeout_message="the wait budget ran out while an LLM was answering",
        )
        async with aclosing(routed) as results:
            return await anext(results)

    async def _route(
        self,
        attempt: Callable[..., AsyncIterator[_Produced]],
        *,
        operation: str | None,
        trace_id: str | None,
        wait: float | None,
        timeout_message: str,
    ) -> AsyncIterator[_Produced]:
        """Run one call over the pool, failing over between LLMs: acquire a
        candidate, pass on whatever its attempt produces, and act on the verdict
        it leaves behind.

        The sole owner of the failover decisions — a streaming attempt produces
        deltas and a non-streaming one a single result, but which candidate comes
        next, and which error the caller finally sees, is decided only here.
        """
        queue_deadline = None if wait is None else time.monotonic() + wait
        # wait=0 is "do not queue", not "answer instantly": it bounds slot
        # acquisition only, leaving the attempt on the global ceiling. How far the
        # deadline then reaches into the attempt is the attempt's own call.
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

            outcome = _Outcome()
            async with aclosing(
                attempt(
                    config,
                    outcome,
                    answer_deadline,
                    operation=operation,
                    trace_id=trace_id,
                ),
            ) as produced:
                async for item in produced:
                    yield item
            if outcome.answered:
                return
            if isinstance(outcome.verdict, _BudgetExpired):
                # A request an earlier LLM already rejected as malformed stays the
                # more useful answer than "the clock ran out" — the caller can act on it.
                if last_client_error is not None:
                    raise last_client_error from None
                raise NoLLMAvailableError(timeout_message, reason="timeout")
            if isinstance(outcome.verdict, _Failed):
                client_failed.add(config.name)
                if outcome.verdict.error is not None:
                    last_client_error = outcome.verdict.error
            # None, or _Failed with no error ⇒ loop to the next free LLM.

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

    async def _spent_budget(self, attempt: _Attempt, outcome: _Outcome) -> None:
        """The caller's ``wait`` was already gone before a request could be opened:
        hand the slot back and journal it, blaming the clock rather than the LLM."""
        await self._pool.release(attempt.config)
        await self._record(attempt, CallStatus.ERROR, error_detail="wait budget exhausted")
        outcome.verdict = _BudgetExpired()

    async def _attempt(  # noqa: PLR0913
        self,
        config: LLMConfig,
        outcome: _Outcome,
        answer_deadline: float | None,
        *,
        messages: list[dict],
        tools: list[dict] | None,
        operation: str | None,
        trace_id: str | None,
    ) -> AsyncIterator[AsyncResult]:
        """Run one LLM and yield its single result, or leave on ``outcome`` the
        verdict the driver fails over on: ``None`` to try the next LLM after a
        cooldown, ``_Failed`` to try the next without cooling this one, or
        ``_BudgetExpired`` when the caller's own ``wait`` ran out."""
        attempt = self._new_attempt(config, operation=operation, trace_id=trace_id)
        backoff = self._backoff(config.name)

        if self._http is None:
            self._http = chat.make_client()

        timeout, budget_bound = self._attempt_timeout(answer_deadline)
        if budget_bound and timeout == 0.0:
            await self._spent_budget(attempt, outcome)
            return

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
            outcome.answered = True
            # Outside the `try` on purpose: the consumer closes this generator on
            # the yield, and a GeneratorExit caught above would journal the attempt
            # a second time.
            yield AsyncResult(
                text=content,
                tool_calls=tool_calls,
                usage=usage,
                call_id=attempt.call_id,
                llm_name=config.name,
                operation=operation,
                store=self._store,
                scope=self._scope,
            )
            return

        await self._dispose(attempt, verdict, backoff=backoff, timeout=timeout)
        outcome.verdict = verdict.outcome

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
        routed = self._route(
            partial(self._stream_attempt, messages=messages),
            operation=operation,
            trace_id=trace_id,
            wait=wait,
            timeout_message="the wait budget ran out before any LLM produced a delta",
        )
        async with aclosing(routed) as deltas:
            async for delta in deltas:
                yield delta

    async def _stream_attempt(  # noqa: PLR0913
        self,
        config: LLMConfig,
        outcome: _Outcome,
        first_delta_deadline: float | None,
        *,
        messages: list[dict],
        operation: str | None,
        trace_id: str | None,
    ) -> AsyncIterator[str]:
        """Stream one LLM, yielding its deltas.

        Leaves a verdict on ``outcome`` when it died before the first delta — the
        slot is settled and journaled by then, so only the next candidate is left
        to decide.
        """
        attempt = self._new_attempt(config, operation=operation, trace_id=trace_id)
        backoff = self._backoff(config.name)
        if self._http is None:
            self._http = chat.make_client()

        timeout, budget_bound = self._attempt_timeout(first_delta_deadline)
        if budget_bound and timeout == 0.0:
            await self._spent_budget(attempt, outcome)
            return

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
                outcome,
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
            outcome.answered = True

    async def _fail_stream(  # noqa: PLR0913
        self,
        attempt: _Attempt,
        exc: Exception,
        outcome: _Outcome,
        *,
        backoff: float,
        timeout: float,
        budget_bound: bool,
        started: bool,
    ) -> None:
        """Settle a failed streaming attempt through the shared failure surface.
        Before the first delta it reports the same verdict a ``chat`` attempt
        would; once deltas have reached the caller nothing can rescue it, so it
        raises ``StreamInterruptedError`` instead."""
        verdict = _classify(exc, budget_bound=budget_bound and not started)
        await self._dispose(attempt, verdict, backoff=backoff, timeout=timeout)
        if started:
            raise StreamInterruptedError(
                f"{attempt.config.name}: the stream died after it had already emitted"
                " deltas — no failover is possible once output has reached the caller",
                llm_name=attempt.config.name,
            ) from exc
        outcome.verdict = verdict.outcome

    async def _log_call(self, call: Call) -> None:
        try:
            await self._store.record(call)
        except Exception:  # noqa: BLE001
            logger.exception("llmbroker: store.record failed")

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
