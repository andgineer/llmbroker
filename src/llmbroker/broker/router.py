"""Router: route one completion over the pool with per-LLM failover, journaling
every attempt. How each failure is disposed of is the contract in call-path.md."""

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
from llmbroker.broker.keyring import KeyRing
from llmbroker.broker.learning import Learner
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.result import AsyncResult, CallReceipt
from llmbroker.chat import (
    aiter_chat_chunks,
    build_chat_request,
    call_provider,
    parse_stream_chunk,
    retry_after_seconds,
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
from llmbroker.models import Call, CallStatus, LLMConfig, Usage
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
    """Identity of one in-flight attempt: what its journal row is keyed by, and the
    ring whose key is paying for it."""

    config: LLMConfig
    call_id: str
    t0: float
    operation: str | None
    trace_id: str | None
    ring: KeyRing
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

    receipt: CallReceipt
    llm_name: str
    call_id: str
    started: bool = False
    usage: Usage | None = None

    def opened(self) -> None:
        """The first delta: what answered stops moving here, so the caller's handle can
        name it while the rest of the answer is still arriving."""
        self.started = True
        self.receipt.llm_name = self.llm_name
        self.receipt.call_id = self.call_id

    def settled(self) -> Usage | None:
        """This attempt is over and its counts are final — hand them to the caller's
        handle as well as to the journal row."""
        self.receipt.usage = self.usage
        return self.usage


async def _stream_deltas(
    client: httpx.AsyncClient,
    request: tuple[str, dict[str, str], dict],
    *,
    model: str,
    timeout: float,
    progress: _StreamProgress,
) -> AsyncIterator[str]:
    """Open one streaming request and yield its text deltas, recording progress.
    ``timeout`` bounds the first delta only, so a slow *consumer* suspending this
    generator can never trip a deadline."""
    url, headers, body = request
    async with (
        asyncio.timeout(timeout) as bound,
        client.stream("POST", url, headers=headers, json=body) as resp,
    ):
        if resp.status_code >= ERROR_FLOOR:
            await resp.aread()
            resp.raise_for_status()
        async for chunk in aiter_chat_chunks(resp, model):
            delta, usage = parse_stream_chunk(chunk, model)
            progress.usage = usage or progress.usage
            if not delta:
                continue
            if not progress.started:
                progress.opened()
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
        # No cooldown: the key is dead, the model is not. Withdrawing the model would
        # take it from every other caller over one caller's rejected key.
        return _Verdict(CallStatus.ERROR, detail, http_status=code, outcome=_Failed(error=None))
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
        optimizer: Optimizer | None = None,
        learner: Learner | None = None,
    ) -> None:
        self._pool = pool
        self._store = store
        self._optimizer = optimizer
        self._learner = learner
        self._http_client: httpx.AsyncClient | None = None

    @property
    def http(self) -> httpx.AsyncClient:
        """The installation's one HTTP client, opened on first use and shared by every
        caller — routed calls and ``direct`` clients alike."""
        if self._http_client is None:
            self._http_client = chat.make_client()
        return self._http_client

    async def ask(
        self,
        ring: KeyRing,
        prompt: str,
        *,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
    ) -> AsyncResult:
        return await self.chat(
            ring,
            [{"role": "user", "content": prompt}],
            operation=operation,
            trace_id=trace_id,
            wait=wait,
        )

    async def chat(  # noqa: PLR0913 - who calls, what, and the three call knobs
        self,
        ring: KeyRing,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
    ) -> AsyncResult:
        routed = self._route(
            partial(self._attempt, messages=messages, tools=tools),
            ring=ring,
            operation=operation,
            trace_id=trace_id,
            wait=wait,
            timeout_message="the wait budget ran out while an LLM was answering",
        )
        async with aclosing(routed) as results:
            return await anext(results)

    async def _route(  # noqa: PLR0913 - one call's whole context: who, what, how long
        self,
        attempt: Callable[..., AsyncIterator[_Produced]],
        *,
        ring: KeyRing,
        operation: str | None,
        trace_id: str | None,
        wait: float | None,
        timeout_message: str,
    ) -> AsyncIterator[_Produced]:
        """Run one call over the pool, failing over between LLMs. The sole owner of
        the failover decisions: which candidate comes next, and which error the
        caller finally sees, is decided only here."""
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
                    payable=await ring.payable(c.api_key_ref for c in self._pool.configs.values()),
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
                    ring=ring,
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

    async def _new_attempt(
        self,
        config: LLMConfig,
        ring: KeyRing,
        *,
        operation: str | None,
        trace_id: str | None,
    ) -> "_Attempt | None":
        """``None`` where the caller's key for this model has gone since the slot was
        taken — a 401 in another attempt is enough, and the slot goes straight back."""
        key = await ring.resolve(config.api_key_ref)
        if key is None:
            await self._pool.release(config)
            return None
        return _Attempt(
            config=config,
            call_id=str(uuid.uuid4()),
            t0=time.monotonic(),
            operation=operation,
            trace_id=trace_id,
            ring=ring,
            resolved_key=key,
        )

    def _backoff(self, name: str) -> float:
        # Read before the first record is awaited (which increments rl_fail_count via
        # the learner), so the first failure in a streak always sees exponent 0.
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
        budget_ms: int | None = None,
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
                scope=attempt.ring.scope,
                cooldown_until=cooldown_until,
                budget_ms=budget_ms,
            ),
        )

    async def _finish_ok(self, attempt: _Attempt, usage: Usage | None) -> None:
        await self._pool.release(attempt.config)
        self._pool.clear_cooling(attempt.config.name)
        self._pool.clear_budget_bound(attempt.config.name)
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
        budget_ms: int | None = None
        if verdict.http_status is not None and is_auth_failure(verdict.http_status):
            attempt.ring.forget(attempt.config.api_key_ref)
        if verdict.cool_base is None:
            await self._pool.release(attempt.config)
            if isinstance(verdict.outcome, _BudgetExpired):
                # Applied as well as journaled: the next caller on this node must not
                # wait on a rebuild, nor on learning being switched on.
                budget_ms = int(timeout * 1000)
                self._pool.raise_budget_bound(attempt.config.name, timeout, datetime.now(UTC))
        else:
            delay = self._capped_wait(verdict.cool_base, backoff)
            await self._pool.cool_down(attempt.config, delay)
        await self._record(
            attempt,
            verdict.status,
            http_status=verdict.http_status,
            error_detail=verdict.detail,
            cooldown_delay=delay,
            budget_ms=budget_ms,
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
        ring: KeyRing,
        messages: list[dict],
        tools: list[dict] | None,
        operation: str | None,
        trace_id: str | None,
    ) -> AsyncIterator[AsyncResult]:
        """Run one LLM and yield its single result, or leave on ``outcome`` the verdict
        the driver fails over on."""
        attempt = await self._new_attempt(config, ring, operation=operation, trace_id=trace_id)
        if attempt is None:
            outcome.verdict = _Failed(error=None)
            return
        backoff = self._backoff(config.name)

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
                    client=self.http,
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
            # Outside the `try`: the consumer closes this generator on the yield, and
            # a GeneratorExit caught above would journal the attempt twice.
            yield AsyncResult(
                text=content,
                tool_calls=tool_calls,
                usage=usage,
                call_id=attempt.call_id,
                llm_name=config.name,
                operation=operation,
                store=self._store,
                scope=ring.scope,
                observe_quality=(
                    self._learner.record_quality_observed if self._learner is not None else None
                ),
            )
            return

        await self._dispose(attempt, verdict, backoff=backoff, timeout=timeout)
        outcome.verdict = verdict.outcome

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def stream(  # noqa: PLR0913 - the chat knobs plus the caller's receipt
        self,
        ring: KeyRing,
        messages: list[dict],
        *,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
        receipt: CallReceipt | None = None,
    ) -> AsyncIterator[str]:
        """Route a streaming completion over the pool, yielding text deltas and naming
        what answered on ``receipt``. Fails over exactly like ``chat`` up to the first
        delta; past it a death raises ``StreamInterruptedError`` instead."""
        routed = self._route(
            partial(
                self._stream_attempt,
                messages=messages,
                receipt=receipt if receipt is not None else CallReceipt(),
            ),
            ring=ring,
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
        ring: KeyRing,
        messages: list[dict],
        operation: str | None,
        trace_id: str | None,
        receipt: CallReceipt,
    ) -> AsyncIterator[str]:
        """Stream one LLM, yielding its deltas. Leaves a verdict on ``outcome`` when it
        died before the first delta; the slot is settled and journaled by then."""
        attempt = await self._new_attempt(config, ring, operation=operation, trace_id=trace_id)
        if attempt is None:
            outcome.verdict = _Failed(error=None)
            return
        backoff = self._backoff(config.name)
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
        progress = _StreamProgress(receipt, config.name, attempt.call_id)
        try:
            async with aclosing(
                _stream_deltas(
                    self.http,
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
            await self._finish_ok(attempt, progress.settled())
            raise
        except BaseException as exc:
            await self._pool.release(config)
            if isinstance(exc, Exception):
                await self._record(attempt, CallStatus.ERROR, error_detail=type(exc).__name__)
            raise
        else:
            await self._finish_ok(attempt, progress.settled())
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
        """Settle a failed streaming attempt through the shared failure surface: the
        same verdict a ``chat`` attempt would give, or ``StreamInterruptedError``
        once deltas have already reached the caller."""
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
        # Guarded separately and reached even when the write failed: a journal nobody
        # can write must not also blind the pool to what just happened.
        if self._learner is not None:
            try:
                await self._learner.observe(call)
            except Exception:  # noqa: BLE001
                logger.exception("llmbroker: learning from the call failed")

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
