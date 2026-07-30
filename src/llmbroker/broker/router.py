"""Router: route one completion over the pool with per-LLM failover.

Acquires a free slot, calls the provider, and journals every attempt; each
failure's disposal — cool down and retry, fail over without cooling, or hand the
caller back its own expired ``wait`` — is the error contract in architecture.md.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from llmbroker import chat
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.result import AsyncResult
from llmbroker.chat import call_provider, is_rate_limit, retry_after_seconds
from llmbroker.exceptions import InvalidProviderResponseError, NoLLMAvailableError
from llmbroker.models import Call, CallStatus, LLMConfig, Usage, key_hash
from llmbroker.optimizer import Optimizer
from llmbroker.protocols.store import StoreProtocol

HTTP_429 = 429
HTTP_401 = 401
HTTP_403 = 403
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
        call_id = str(uuid.uuid4())
        t0 = time.monotonic()
        resolved_key = self._pool.resolved_key(config.name)

        async def record(  # noqa: PLR0913
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
                    id=call_id,
                    llm_name=config.name,
                    operation=operation,
                    trace_id=trace_id,
                    status=status,
                    ts=datetime.now(UTC),
                    http_status=http_status,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    error_detail=error_detail,
                    usage=usage,
                    scope=self._scope,
                    cooldown_until=cooldown_until,
                    key_hash=key_hash(resolved_key) if cooldown_delay is not None else None,
                ),
            )

        # Read before record() is awaited (which increments rl_fail_count via
        # the learning hook), so the first failure in a streak always sees exponent 0.
        fails_before = self._optimizer.rl_fail_count(config.name) if self._optimizer else 0
        backoff = self._optimizer.backoff_factor**fails_before if self._optimizer else 1.0

        if self._http is None:
            self._http = chat.make_client()

        timeout, budget_bound = self._attempt_timeout(answer_deadline)
        if budget_bound and timeout == 0.0:
            await self._pool.release(config)
            await record(CallStatus.ERROR, error_detail="wait budget exhausted")
            return _BudgetExpired()

        try:
            # httpx applies its timeout per operation (connect, write, read), so
            # only this wall-clock bound keeps the whole attempt inside the budget.
            async with asyncio.timeout(timeout):
                content, tool_calls, usage = await call_provider(
                    config,
                    resolved_key,
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
                await record(CallStatus.ERROR, error_detail=type(exc).__name__)
            raise
        else:
            await self._pool.release(config)
            self._pool.clear_cooling(config.name)
            self._pool.clear_unmet_budget(config.name)
            await record(CallStatus.OK, http_status=200, usage=usage)
            return AsyncResult(
                text=content,
                tool_calls=tool_calls,
                usage=usage,
                call_id=call_id,
                llm_name=config.name,
                operation=operation,
                store=self._store,
                scope=self._scope,
            )

        delay: float | None = None
        if verdict.cool_base is None:
            await self._pool.release(config)
            if isinstance(verdict.outcome, _BudgetExpired):
                # Not a penalty: the only latency this model will ever report is the
                # budget it just failed to meet, and the next caller offering no more
                # than that should be handed a sibling first.
                self._pool.note_unmet_budget(config, timeout)
        else:
            delay = self._capped_wait(verdict.cool_base, backoff)
            await self._pool.cool_down(config, delay)
        await record(
            verdict.status,
            http_status=verdict.http_status,
            error_detail=verdict.detail,
            cooldown_delay=delay,
        )
        return verdict.outcome

    async def _log_call(self, call: Call) -> None:
        try:
            await self._store.record(call)
        except Exception:  # noqa: BLE001
            logger.exception("llmbroker: store.record failed")

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
