"""Provider-facing streaming attempts and their per-lane state."""

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from contextlib import AsyncExitStack, aclosing
from dataclasses import dataclass, field
from typing import Any, NoReturn, TypeVar, cast

import httpx

from llmbroker import chat
from llmbroker.broker import verdict as verdicts
from llmbroker.broker.keyring import KeyRing
from llmbroker.broker.result import AsyncResult, CallReceipt, ObserveQuality
from llmbroker.broker.route_state import Attempt, Outcome, RouteCall
from llmbroker.broker.verdict import FAILOVER_ERRORS, BudgetExpired, Failed, Verdict
from llmbroker.chat import (
    NO_DELTA,
    aiter_chat_chunks,
    build_chat_request,
    empty_answer_error,
    parse_stream_chunk,
)
from llmbroker.exceptions import LLMTimeoutError, StreamInterruptedError
from llmbroker.http_status import ERROR_FLOOR
from llmbroker.models import CallStatus, LLMConfig, Usage
from llmbroker.protocols.store import StoreProtocol

Produced = TypeVar("Produced")


@dataclass(frozen=True, slots=True)
class StreamBackend:
    """The narrow router services needed by streamed calls."""

    configs: Callable[[], Iterable[str]]
    payable: Callable[[KeyRing], Awaitable[frozenset[str]]]
    acquire: Callable[..., Awaitable[list[LLMConfig]]]
    produce: Callable[[RouteCall, LLMConfig, Outcome], AsyncGenerator[Any, None]]
    untried: Callable[..., Awaitable[list[LLMConfig]]]
    publish: Callable[[CallReceipt | None, Any], None]
    cancel: Callable[[Any], None]
    stop: Callable[[Any], Awaitable[None]]
    settled: Callable[[RouteCall], Awaitable[None]]
    new_attempt: Callable[..., Awaitable[Attempt | None]]
    backoff: Callable[[str], float]
    attempt_timeout: Callable[[float | None], tuple[float, bool]]
    spent_budget: Callable[[Attempt, Outcome], Awaitable[None]]
    settle_superseded: Callable[[Attempt, Usage | None], Awaitable[None]]
    finish_ok: Callable[[Attempt, Usage | None], Awaitable[None]]
    release: Callable[[LLMConfig], Awaitable[None]]
    record: Callable[..., Awaitable[None]]
    dispose: Callable[..., Awaitable[None]]
    http: Callable[[], httpx.AsyncClient]
    store: StoreProtocol
    observe_quality: ObserveQuality | None


class BudgetExhaustedError(Exception):
    """The caller's budget ran out after a streamed answer had started."""


@dataclass(slots=True)
class StreamLane:
    """One provider attempt retained by a streamed call."""

    config: LLMConfig
    outcome: Outcome
    produced: AsyncGenerator[str, None]
    task: asyncio.Task[None] | None = None
    deltas: list[str] = field(default_factory=list)
    sent: int = 0
    first_delta_at: float | None = None
    failure: BaseException | None = None
    finished: bool = False
    absorbed: bool = False
    retired: bool = False
    opening_order: int = 0
    hold_first: bool = False
    consumer_driven: bool = False
    release_first: asyncio.Event = field(default_factory=asyncio.Event)
    pulled: asyncio.Event = field(default_factory=asyncio.Event)
    wave: int = 0


@dataclass(slots=True)
class StreamRace:
    """Retained lanes and the provisional lane selected for initial deltas."""

    lanes: list[StreamLane]
    width: int
    deadline: float = 0.0
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    exposed: StreamLane | None = None
    done: bool = False

    @property
    def preferred(self) -> StreamLane:
        return self.lanes[0]

    def live(self) -> list[StreamLane]:
        return [lane for lane in self.lanes if not lane.finished]

    def winner(self) -> StreamLane | None:
        """Return the lane whose provider completed a valid answer first."""
        best: StreamLane | None = None
        best_key = (0.0, 0)
        for lane in self.lanes:
            at = lane.outcome.completed_at
            key = (at, lane.opening_order) if at is not None else None
            if key is not None and (best is None or key < best_key):
                best, best_key = lane, key
        return best

    def select(self) -> StreamLane | None:
        """Choose whose deltas become the provisional initial stream."""
        preferred = self.preferred
        if preferred.first_delta_at is not None and preferred.first_delta_at <= self.deadline:
            return preferred
        if not preferred.finished and time.monotonic() < self.deadline:
            return None
        best: StreamLane | None = None
        best_at = 0.0
        for lane in self.lanes:
            at = lane.first_delta_at
            if at is not None and (best is None or at < best_at):
                best, best_at = lane, at
        return best

    def timeout(self) -> float | None:
        if self.exposed is not None or self.preferred.finished:
            return None
        remaining = self.deadline - time.monotonic()
        return remaining if remaining > 0 else None


@dataclass(slots=True)
class StreamProgress:
    """Identity and usage observed during one streaming attempt."""

    receipt: CallReceipt
    llm_name: str
    call_id: str
    started: bool = False
    usage: Usage | None = None

    def opened(self) -> None:
        self.started = True
        self.receipt.llm_name = self.llm_name
        self.receipt.call_id = self.call_id

    def settle(self) -> None:
        self.receipt.llm_name = self.llm_name
        self.receipt.call_id = self.call_id
        self.receipt.usage = self.usage
        self.receipt.settled = True


@dataclass(slots=True)
class StreamBudget:
    """The mutable deadline shared by every attempt owned by one stream."""

    deadline: float | None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    paused_at: float | None = None

    def pause(self) -> None:
        if self.deadline is not None and self.paused_at is None:
            self.paused_at = time.monotonic()
            self.changed.set()

    def resume(self) -> float:
        if self.paused_at is None:
            return 0.0
        elapsed = time.monotonic() - self.paused_at
        self.paused_at = None
        self.deadline = cast(float, self.deadline) + elapsed
        self.changed.set()
        return elapsed


async def budgeted_await(
    awaited: Awaitable[Produced],
    provider_deadline: float,
    budget: StreamBudget | None,
) -> Produced:
    """Await provider I/O against a deadline an ordinary reader may pause."""
    task = asyncio.ensure_future(awaited)
    try:
        while True:
            if budget is None:
                async with asyncio.timeout_at(provider_deadline):
                    return await task
            budget.changed.clear()
            deadline = provider_deadline
            if budget.deadline is not None and budget.paused_at is None:
                deadline = min(deadline, budget.deadline)
            changed = asyncio.create_task(budget.changed.wait())
            done, _ = await asyncio.wait(
                (task, changed),
                timeout=max(deadline - time.monotonic(), 0.0),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                changed.cancel()
                await asyncio.gather(changed, return_exceptions=True)
                return task.result()
            if changed in done:
                await changed
                continue
            changed.cancel()
            task.cancel()
            await asyncio.gather(changed, task, return_exceptions=True)
            raise TimeoutError
    except BaseException:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise


async def stream_deltas(  # noqa: PLR0913
    client: httpx.AsyncClient,
    request: tuple[str, dict[str, str], dict],
    *,
    model: str,
    timeout: float,
    progress: StreamProgress,
    budget: StreamBudget | None = None,
) -> AsyncGenerator[str, None]:
    """Open one streaming request and yield its text deltas."""
    url, headers, body = request
    loop = asyncio.get_running_loop()
    provider_deadline = loop.time() + timeout
    try:
        async with AsyncExitStack() as opened:
            resp = await budgeted_await(
                opened.enter_async_context(client.stream("POST", url, headers=headers, json=body)),
                provider_deadline,
                budget,
            )
            if resp.status_code >= ERROR_FLOOR:
                await resp.aread()
                resp.raise_for_status()
            chunks = await opened.enter_async_context(aclosing(aiter_chat_chunks(resp, model)))
            while True:
                try:
                    chunk = await budgeted_await(anext(chunks), provider_deadline, budget)
                except StopAsyncIteration:
                    break
                delta, usage = parse_stream_chunk(chunk, model)
                progress.usage = usage or progress.usage
                if not delta:
                    continue
                if not progress.started:
                    progress.opened()
                held = loop.time()
                yield delta
                provider_deadline += loop.time() - held
    except TimeoutError:
        if progress.started:
            raise BudgetExhaustedError from None
        raise


async def drain_lane(lane: StreamLane, wake: asyncio.Event) -> None:
    """Read one lane fully so consumer pacing cannot decide its completion time."""
    try:
        async for delta in lane.produced:
            if lane.first_delta_at is None:
                lane.first_delta_at = time.monotonic()
            lane.deltas.append(delta)
            wake.set()
    except Exception as exc:  # noqa: BLE001
        lane.failure = exc
    finally:
        lane.finished = True
        wake.set()


def name(receipt: CallReceipt, lane: StreamLane) -> None:
    """Name the provisional lane without marking the handle settled."""
    receipt.llm_name = lane.outcome.receipt.llm_name
    receipt.call_id = lane.outcome.receipt.call_id


def replacement(backend: StreamBackend, call: RouteCall, winner: StreamLane) -> AsyncResult:
    """Build the complete result belonging to a retained lane."""
    return AsyncResult(
        text="".join(winner.deltas),
        tool_calls=None,
        usage=winner.outcome.receipt.usage,
        call_id=cast(str, winner.outcome.receipt.call_id),
        llm_name=winner.config.name,
        operation=call.operation,
        store=backend.store,
        scope=call.ring.scope,
        observe_quality=backend.observe_quality,
    )


async def stream_attempt(  # noqa: PLR0913
    backend: StreamBackend,
    config: LLMConfig,
    outcome: Outcome,
    first_delta_deadline: float | None,
    *,
    ring: KeyRing,
    messages: list[dict],
    operation: str | None,
    trace_id: str | None,
    response_format: dict | None = None,
    budget: StreamBudget | None = None,
) -> AsyncGenerator[str, None]:
    """Stream one provider and settle its slot, row, and outcome."""
    attempt = await backend.new_attempt(
        config,
        ring,
        outcome,
        operation=operation,
        trace_id=trace_id,
    )
    if attempt is None:
        outcome.verdict = Failed(error=None)
        return
    backoff = backend.backoff(config.name)
    timeout, budget_bound = backend.attempt_timeout(first_delta_deadline)
    if budget_bound and timeout == 0.0:
        outcome.settling = True
        await backend.spent_budget(attempt, outcome)
        return

    request = build_chat_request(
        config.base_url,
        config.model,
        attempt.resolved_key,
        messages,
        stream=True,
        params=None if response_format is None else {"response_format": response_format},
    )
    progress = StreamProgress(outcome.receipt, config.name, attempt.call_id)
    try:
        async with aclosing(
            stream_deltas(
                backend.http(),
                request,
                model=config.name,
                timeout=chat.HTTP_TIMEOUT if budget is not None else timeout,
                progress=progress,
                budget=budget,
            ),
        ) as deltas:
            async for delta in deltas:
                yield delta
    except BudgetExhaustedError:
        outcome.settling = True
        outcome.verdict = BudgetExpired()
        await exhausted(backend, attempt, timeout)
    except FAILOVER_ERRORS as exc:
        outcome.settling = True
        await fail_stream(
            backend,
            attempt,
            exc,
            outcome,
            backoff=backoff,
            timeout=timeout,
            budget_bound=budget_bound,
            started=progress.started,
        )
    except GeneratorExit:
        await stream_stopped(backend, attempt, progress, outcome)
        raise
    except BaseException as exc:
        outcome.settling = True
        await stream_aborted(backend, attempt, progress, outcome, exc)
        raise
    else:
        outcome.settling = True
        await settle_stream(
            backend,
            attempt,
            progress,
            outcome,
            backoff=backoff,
            timeout=timeout,
        )


async def stream_stopped(
    backend: StreamBackend,
    attempt: Attempt,
    progress: StreamProgress,
    outcome: Outcome,
) -> None:
    if outcome.superseded:
        await backend.settle_superseded(attempt, progress.usage)
        return
    await backend.finish_ok(attempt, progress.usage)
    progress.settle()


async def stream_aborted(
    backend: StreamBackend,
    attempt: Attempt,
    progress: StreamProgress,
    outcome: Outcome,
    exc: BaseException,
) -> None:
    if outcome.superseded or outcome.stopped:
        await stream_stopped(backend, attempt, progress, outcome)
        return
    await backend.release(attempt.config)
    if isinstance(exc, Exception):
        outcome.crashed = exc
        await backend.record(attempt, CallStatus.ERROR, error_detail=type(exc).__name__)


async def settle_stream(  # noqa: PLR0913
    backend: StreamBackend,
    attempt: Attempt,
    progress: StreamProgress,
    outcome: Outcome,
    *,
    backoff: float,
    timeout: float,
) -> None:
    if not progress.started:
        verdict = verdicts.classify(
            empty_answer_error(attempt.config.name, NO_DELTA),
            budget_bound=False,
        )
        await backend.dispose(attempt, verdict, backoff=backoff, timeout=timeout)
        outcome.verdict = verdict.outcome
        return
    outcome.completed_at = time.monotonic()
    if outcome.completed is not None:
        outcome.completed()
    await backend.finish_ok(attempt, progress.usage)
    progress.settle()
    outcome.answered = True


async def exhausted(backend: StreamBackend, attempt: Attempt, timeout: float) -> NoReturn:
    elapsed = time.monotonic() - attempt.t0
    await backend.dispose(
        attempt,
        Verdict(
            CallStatus.ERROR,
            f"answer budget exhausted after {elapsed:.1f}s",
            outcome=BudgetExpired(),
        ),
        backoff=backend.backoff(attempt.config.name),
        timeout=timeout,
    )
    raise LLMTimeoutError(
        f"{attempt.config.name}: the answer was still arriving {elapsed:.1f}s in, past"
        " the budget, and nothing can be retried once output has reached the caller",
    )


async def fail_stream(  # noqa: PLR0913
    backend: StreamBackend,
    attempt: Attempt,
    exc: Exception,
    outcome: Outcome,
    *,
    backoff: float,
    timeout: float,
    budget_bound: bool,
    started: bool,
) -> None:
    verdict = verdicts.classify(exc, budget_bound=budget_bound and not started)
    await backend.dispose(attempt, verdict, backoff=backoff, timeout=timeout)
    outcome.verdict = verdict.outcome
    if started:
        raise StreamInterruptedError(
            f"{attempt.config.name}: the stream died after it had already emitted"
            " deltas — no failover is possible once output has reached the caller",
            llm_name=attempt.config.name,
        ) from exc
