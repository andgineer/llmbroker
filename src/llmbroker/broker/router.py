"""Router: route one completion over the pool with per-LLM failover.

Acquires a free slot, calls the provider, and on a 429/503/401/403/5xx cools
that LLM down (or drops it) and tries the next free one; a client-side 4xx
(any other status in [400, 500)) fails over without cooling and, if every
candidate is exhausted this way, surfaces the provider error to the caller.
Every attempt is recorded to the journal.
"""

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
from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.models import Call, CallStatus, LLMConfig, Usage, key_hash
from llmbroker.optimizer import Optimizer
from llmbroker.protocols.store import StoreProtocol

HTTP_429 = 429
HTTP_401 = 401
HTTP_403 = 403
_DEFAULT_RATE_LIMIT_SEC = 60

logger = logging.getLogger("llmbroker.broker")


def _is_client_error(code: int) -> bool:
    return 400 <= code < 500 and code not in (HTTP_429, HTTP_401, HTTP_403)  # noqa: PLR2004


@dataclass(frozen=True)
class _Failed:
    """One candidate is done for this request; ``error`` is set only for a
    genuine client error (never for a dead key), so the router can decide
    whether to surface it once every candidate is exhausted."""

    error: httpx.HTTPStatusError | None


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
        deadline = None if wait is None else time.monotonic() + wait
        client_failed: set[str] = set()
        last_client_error: httpx.HTTPStatusError | None = None
        while True:
            try:
                config = await self._pool.acquire(
                    deadline,
                    operation=operation,
                    exclude=frozenset(client_failed),
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
            )
            if isinstance(outcome, AsyncResult):
                return outcome
            if isinstance(outcome, _Failed):
                client_failed.add(config.name)
                if outcome.error is not None:
                    last_client_error = outcome.error
            # None or _Failed with no error ⇒ loop to the next free LLM.

    def _capped_wait(self, base: float, backoff: float) -> float:
        cap = self._optimizer.max_delay if self._optimizer else base
        return min(base * backoff, cap)

    async def _attempt(
        self,
        config: LLMConfig,
        messages: list[dict],
        tools: list[dict] | None,
        *,
        operation: str | None,
        trace_id: str | None,
    ) -> AsyncResult | _Failed | None:
        """Run one LLM. Return its result, ``None`` to try the next LLM after a
        cooldown, or ``_Failed`` to try the next LLM without cooling this one."""
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

        try:
            content, tool_calls, usage = await call_provider(
                config,
                resolved_key,
                messages,
                tools,
                client=self._http,
            )
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            detail = exc.response.text[:300]
            if is_rate_limit(code):
                status = CallStatus.RATE_LIMITED if code == HTTP_429 else CallStatus.UNAVAILABLE
                base = retry_after_seconds(exc.response.headers, _DEFAULT_RATE_LIMIT_SEC)
                delay = self._capped_wait(base, backoff)
                await self._pool.cool_down(config, delay)
                await record(status, http_status=code, error_detail=detail, cooldown_delay=delay)
                return None
            if code in (HTTP_401, HTTP_403):
                delay = self._capped_wait(_DEFAULT_RATE_LIMIT_SEC, backoff)
                await self._pool.cool_down(config, delay)
                await record(
                    CallStatus.ERROR,
                    http_status=code,
                    error_detail=detail,
                    cooldown_delay=delay,
                )
                return _Failed(error=None)
            if _is_client_error(code):
                await self._pool.release(config)
                await record(CallStatus.ERROR, http_status=code, error_detail=detail)
                return _Failed(error=exc)
            delay = self._capped_wait(_DEFAULT_RATE_LIMIT_SEC, backoff)
            await self._pool.cool_down(config, delay)
            await record(
                CallStatus.ERROR,
                http_status=code,
                error_detail=detail,
                cooldown_delay=delay,
            )
            return None
        except (httpx.TimeoutException, httpx.ConnectError, OSError) as exc:
            delay = self._capped_wait(_DEFAULT_RATE_LIMIT_SEC, backoff)
            await self._pool.cool_down(config, delay)
            await record(CallStatus.ERROR, error_detail=type(exc).__name__, cooldown_delay=delay)
            return None

        await self._pool.release(config)
        self._pool.clear_cooling(config.name)
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

    async def _log_call(self, call: Call) -> None:
        try:
            await self._store.record(call)
        except Exception:  # noqa: BLE001
            logger.exception("llmbroker: store.record failed")

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
