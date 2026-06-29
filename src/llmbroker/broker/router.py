"""Router: route one completion over the pool with per-LLM failover.

Acquires a free slot, calls the provider, and on a 429/503 cools that LLM down
and tries the next free one; every attempt is recorded to telemetry.
"""

import asyncio
import logging
import time
import uuid

import httpx

from llmbroker.broker.pool import LLMPool
from llmbroker.broker.result import AsyncResult
from llmbroker.chat import call_provider, is_rate_limit
from llmbroker.exceptions import AllLLMsFailedError, NoLLMAvailableError
from llmbroker.models import Call, CallStatus, LLMConfig, Usage
from llmbroker.optimizer import Optimizer, SelectionPolicy
from llmbroker.protocols.telemetry import TelemetryProtocol

HTTP_429 = 429

logger = logging.getLogger("llmbroker.broker")


class Router:
    """Routes a completion request over the pool, failing over between LLMs."""

    def __init__(
        self,
        pool: LLMPool,
        telemetry: TelemetryProtocol,
        *,
        user_id: int | str | None,
        optimizer: Optimizer | None = None,
        policy: SelectionPolicy | None = None,
    ) -> None:
        self._pool = pool
        self._telemetry = telemetry
        self._user_id = user_id
        self._optimizer = optimizer
        self._policy = policy

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
        while True:
            try:
                config = await self._pool.acquire(wait, policy=self._policy, operation=operation)
            except (asyncio.QueueEmpty, TimeoutError) as exc:
                raise NoLLMAvailableError("no LLM slot came free within wait") from exc

            if config.name not in self._pool:
                # Removed since enqueued — drop the stale slot, try next.
                continue

            if not self._pool.has_key(config.name):
                self._pool.release(config)
                raise AllLLMsFailedError(
                    f"{config.name}: api_key_ref {config.api_key_ref!r} not resolved"
                    " — set the env var or configure a secrets backend",
                )

            if await self._pool.apply_shared_cooling(config):
                continue

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

        async def record(
            status: CallStatus,
            *,
            http_status: int | None = None,
            error_detail: str | None = None,
            usage: Usage | None = None,
        ) -> None:
            await self._log_call(
                Call(
                    id=call_id,
                    llm_name=config.name,
                    operation=operation,
                    trace_id=trace_id,
                    status=status,
                    http_status=http_status,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    error_detail=error_detail,
                    usage=usage,
                    user_id=self._user_id,
                ),
            )

        try:
            content, tool_calls, usage = await call_provider(
                config,
                self._pool.resolved_key(config.name),
                messages,
                tools,
            )
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            detail = exc.response.text[:300]
            if is_rate_limit(code):
                status = CallStatus.RATE_LIMITED if code == HTTP_429 else CallStatus.UNAVAILABLE
                delay = self._optimizer.delay_for(config.name) if self._optimizer else None
                await self._pool.cool_down(config, exc.response.headers, delay_override=delay)
                await record(status, http_status=code, error_detail=detail)
                if wait == 0:
                    raise NoLLMAvailableError(f"{config.name} rate-limited and wait=0") from exc
                return None
            self._pool.release(config)
            await record(CallStatus.ERROR, http_status=code, error_detail=detail)
            raise AllLLMsFailedError(f"{config.name} returned HTTP {code}") from exc
        except (httpx.TimeoutException, httpx.ConnectError, OSError) as exc:
            self._pool.release(config)
            await record(CallStatus.ERROR, error_detail=type(exc).__name__)
            raise AllLLMsFailedError(
                f"{config.name} network error ({type(exc).__name__})",
            ) from exc

        self._pool.release(config)
        self._pool.clear_cooling(config.name)
        await record(CallStatus.OK, http_status=200, usage=usage)
        return AsyncResult(
            text=content,
            tool_calls=tool_calls,
            usage=usage,
            call_id=call_id,
            llm_name=config.name,
            telemetry=self._telemetry,
            pool=self._pool,
        )

    async def _log_call(self, call: Call) -> None:
        try:
            await self._telemetry.record(call)
        except Exception:  # noqa: BLE001
            logger.exception("llmbroker: telemetry.record failed")
