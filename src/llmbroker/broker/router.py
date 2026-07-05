"""Router: route one completion over the pool with per-LLM failover.

Acquires a free slot, calls the provider, and on a 429/503 cools that LLM down
and tries the next free one; every attempt is recorded to the journal.
"""

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx

from llmbroker.broker.pool import LLMPool
from llmbroker.broker.result import AsyncResult
from llmbroker.chat import call_provider, is_rate_limit, retry_after_seconds
from llmbroker.exceptions import AllLLMsFailedError, NoLLMAvailableError
from llmbroker.models import Call, CallStatus, LLMConfig, Usage, key_hash
from llmbroker.optimizer import Optimizer
from llmbroker.protocols.knowledge import KnowledgeProtocol

HTTP_429 = 429
_DEFAULT_RATE_LIMIT_SEC = 60

logger = logging.getLogger("llmbroker.broker")


class Router:
    """Routes a completion request over the pool, failing over between LLMs."""

    def __init__(
        self,
        pool: LLMPool,
        knowledge: KnowledgeProtocol,
        *,
        scope: str | None,
        optimizer: Optimizer | None = None,
    ) -> None:
        self._pool = pool
        self._knowledge = knowledge
        self._scope = scope
        self._optimizer = optimizer

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
        # Eager, wait-independent: a keyless slot is never available (see pool._available), so a
        # pool with zero keyed configs never has anything to acquire. Without this check, the
        # default wait=None would block forever instead of raising the "zero usable models"
        # error this is meant to guarantee.
        if self._pool.configs and not any(self._pool.has_key(name) for name in self._pool.configs):
            raise AllLLMsFailedError(
                "no LLM has a resolved api_key_ref — set at least one env var or configure"
                " a secrets backend",
            )

        while True:
            try:
                config = await self._pool.acquire(wait, operation=operation)
            except TimeoutError as exc:
                raise NoLLMAvailableError("no LLM slot came free within wait") from exc

            result = await self._attempt(
                config,
                messages,
                tools,
                operation=operation,
                trace_id=trace_id,
                wait=wait,
            )
            if result is not None:
                return result
            # None ⇒ rate-limited; loop to the next free LLM / wait out cooldown.

    def _capped_wait(self, base: float, backoff: float) -> float:
        cap = self._optimizer.max_delay if self._optimizer else base
        return min(base * backoff, cap)

    async def _attempt(  # noqa: PLR0913
        self,
        config: LLMConfig,
        messages: list[dict],
        tools: list[dict] | None,
        *,
        operation: str | None,
        trace_id: str | None,
        wait: float | None,
    ) -> AsyncResult | None:
        """Run one LLM. Return its result, or ``None`` to signal 'try the next LLM'."""
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

        try:
            content, tool_calls, usage = await call_provider(
                config,
                resolved_key,
                messages,
                tools,
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
            else:
                delay = self._capped_wait(_DEFAULT_RATE_LIMIT_SEC, backoff)
                await self._pool.cool_down(config, delay)
                await record(
                    CallStatus.ERROR,
                    http_status=code,
                    error_detail=detail,
                    cooldown_delay=delay,
                )
            if wait == 0:
                raise NoLLMAvailableError(f"{config.name} failed and wait=0") from exc
            return None
        except (httpx.TimeoutException, httpx.ConnectError, OSError) as exc:
            delay = self._capped_wait(_DEFAULT_RATE_LIMIT_SEC, backoff)
            await self._pool.cool_down(config, delay)
            await record(CallStatus.ERROR, error_detail=type(exc).__name__, cooldown_delay=delay)
            if wait == 0:
                raise NoLLMAvailableError(f"{config.name} failed and wait=0") from exc
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
            knowledge=self._knowledge,
            pool=self._pool,
        )

    async def _log_call(self, call: Call) -> None:
        try:
            await self._knowledge.record(call)
        except Exception:  # noqa: BLE001
            logger.exception("llmbroker: knowledge.record failed")
