"""Router: route one completion over the pool with per-LLM failover, journaling
every attempt. How each failure is disposed of is the contract in call-path.md."""

import asyncio
import logging
import math
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, NoReturn, TypeVar

import httpx

from llmbroker import chat
from llmbroker.broker.keyring import KeyRing
from llmbroker.broker.learning import Learner
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.result import AsyncResult, CallReceipt
from llmbroker.broker.route_state import Attempt as _Attempt
from llmbroker.broker.route_state import Lane as _Lane
from llmbroker.broker.route_state import Outcome as _Outcome
from llmbroker.broker.route_state import RouteCall as _Call
from llmbroker.broker.stream_owner import RoutedStream as _RoutedStream
from llmbroker.broker.streaming import StreamBackend
from llmbroker.broker.streaming import StreamLane as _StreamLane
from llmbroker.broker.streaming import StreamRace as _StreamRace  # noqa: F401
from llmbroker.broker.streaming import drain_lane as _drain_stream_lane
from llmbroker.broker.verdict import FAILOVER_ERRORS as _FAILOVER_ERRORS
from llmbroker.broker.verdict import BudgetExpired as _BudgetExpired
from llmbroker.broker.verdict import Failed as _Failed
from llmbroker.broker.verdict import Verdict as _Verdict
from llmbroker.broker.verdict import classify as _classify
from llmbroker.chat import call_provider
from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.http_status import is_auth_failure
from llmbroker.models import Call, CallStatus, LLMConfig, Usage
from llmbroker.optimizer import Optimizer
from llmbroker.protocols.store import StoreProtocol

logger = logging.getLogger("llmbroker.broker")

_Produced = TypeVar("_Produced")


def _check_lanes(fastest_of: int | None, parallel_recovery: bool) -> None:
    """Refuse both parallel options before any provider request opens."""
    if isinstance(fastest_of, bool) or (
        fastest_of is not None and (not isinstance(fastest_of, int) or fastest_of < 1)
    ):
        raise ValueError(f"fastest_of must be None or a positive int, got {fastest_of!r}")
    if not isinstance(parallel_recovery, bool):
        # Both validation knobs have historically used ValueError; keep that public
        # contract even though this particular invalid value also has the wrong type.
        raise ValueError(  # noqa: TRY004
            f"parallel_recovery must be True or False, got {parallel_recovery!r}",
        )


def _check_window(stream_selection_window: float) -> None:
    """Refuse an unusable selection window before any provider request opens."""
    value = stream_selection_window
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(
            f"stream_selection_window must be a finite non-negative number, got {value!r}",
        )


def _request_params(response_format: dict | None) -> dict[str, object] | None:
    """The routed body's extra keys. A caller that asked for nothing sends the body it
    sent before, byte for byte."""
    return None if response_format is None else {"response_format": response_format}


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

    async def ask(  # noqa: PLR0913 - the call knobs, one keyword each
        self,
        ring: KeyRing,
        prompt: str,
        *,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
        fastest_of: int | None = None,
        parallel_recovery: bool = True,
        response_format: dict | None = None,
    ) -> AsyncResult:
        return await self.chat(
            ring,
            [{"role": "user", "content": prompt}],
            operation=operation,
            trace_id=trace_id,
            wait=wait,
            fastest_of=fastest_of,
            parallel_recovery=parallel_recovery,
            response_format=response_format,
        )

    async def chat(  # noqa: PLR0913 - who calls, what, and the call knobs
        self,
        ring: KeyRing,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
        fastest_of: int | None = None,
        parallel_recovery: bool = True,
        response_format: dict | None = None,
    ) -> AsyncResult:
        _check_lanes(fastest_of, parallel_recovery)
        routed = self._route(
            partial(
                self._attempt,
                messages=messages,
                tools=tools,
                response_format=response_format,
            ),
            ring=ring,
            operation=operation,
            trace_id=trace_id,
            wait=wait,
            timeout_message="the wait budget ran out while an LLM was answering",
            fastest_of=fastest_of,
            parallel_recovery=parallel_recovery,
        )
        async with aclosing(routed) as results:
            return await anext(results)

    async def _route(  # noqa: PLR0913 - one call's whole context: who, what, how long
        self,
        attempt: Callable[..., AsyncGenerator[_Produced, None]],
        *,
        ring: KeyRing,
        operation: str | None,
        trace_id: str | None,
        wait: float | None,
        timeout_message: str,
        fastest_of: int | None = None,
        parallel_recovery: bool = True,
        receipt: CallReceipt | None = None,
    ) -> AsyncGenerator[_Produced, None]:
        """Run one call over the pool, failing over between LLMs and racing distinct
        candidates where the caller or the pool's own recovery asks for it. The sole
        owner of which candidate comes next and which error the caller finally sees."""
        queue_deadline = None if wait is None else time.monotonic() + wait
        # wait=0 is "do not queue", not "answer instantly": it bounds slot
        # acquisition only, leaving the attempt on the global ceiling.
        answer_deadline = queue_deadline if wait else None
        call = _Call(
            attempt=attempt,
            ring=ring,
            operation=operation,
            trace_id=trace_id,
            answer_deadline=answer_deadline,
            width=fastest_of if fastest_of is not None and fastest_of > 1 else 1,
            recovery_width=2 if parallel_recovery else 1,
        )
        while True:
            payable = await self._payable(ring)
            configs = await self._acquire(call, queue_deadline, payable)
            lanes = [self._open(call, config) for config in configs]
            winner = await (
                self._alone(call, lanes[0]) if len(lanes) == 1 else self._race(call, lanes)
            )
            if winner is not None:
                try:
                    async with aclosing(winner.produced) as rest:
                        self._publish(receipt, winner)
                        yield winner.first
                        async for item in rest:
                            yield item
                finally:
                    self._publish(receipt, winner)
                    await self._settled(call)
                return
            if call.expired:
                self._expired(call, payable, timeout_message)
            # Every lane failed without answering ⇒ loop to the next free LLM.

    async def _payable(self, ring: KeyRing) -> frozenset[str]:
        return await ring.payable(c.api_key_ref for c in self._pool.configs.values())

    async def _acquire(
        self,
        call: _Call,
        queue_deadline: float | None,
        payable: frozenset[str],
        eligible_names: frozenset[str] | None = None,
    ) -> list[LLMConfig]:
        """Reserve this call's next candidates, or raise what the caller can act on."""
        try:
            configs = await self._pool.acquire_many(
                queue_deadline,
                payable=payable,
                width=call.width,
                recovery_width=call.recovery_width,
                operation=call.operation,
                exclude=frozenset(
                    call.client_failed | (call.attempted if eligible_names is not None else set()),
                ),
                answer_deadline=call.answer_deadline,
                eligible_names=eligible_names,
            )
        except NoLLMAvailableError as exc:
            if exc.reason == "excluded" and call.last_client_error is not None:
                raise call.last_client_error from None
            raise
        if eligible_names is not None:
            call.attempted.update(config.name for config in configs)
        return configs

    def _expired(self, call: _Call, payable: frozenset[str], message: str) -> NoReturn:
        """The budget ran out with nothing answered. A request an earlier LLM already
        rejected as malformed stays the more useful answer than "the clock ran out"."""
        if call.last_client_error is not None:
            raise call.last_client_error from None
        raise NoLLMAvailableError(
            message,
            reason="timeout",
            retry_at=self._pool.retry_at(payable, exclude=frozenset(call.client_failed)),
        )

    def _produce(
        self,
        call: _Call,
        config: LLMConfig,
        outcome: _Outcome,
    ) -> AsyncGenerator[Any, None]:
        return call.attempt(
            config,
            outcome,
            call.answer_deadline,
            ring=call.ring,
            operation=call.operation,
            trace_id=call.trace_id,
        )

    def _open(self, call: _Call, config: LLMConfig) -> _Lane:
        """One candidate's lane, not started until something drives it."""
        outcome = _Outcome()
        return _Lane(config=config, outcome=outcome, produced=self._produce(call, config, outcome))

    async def _alone(self, call: _Call, lane: _Lane) -> _Lane | None:
        """One candidate on the call path, which is what an ordinary healthy call is:
        no task, no competitor, nothing to settle but this attempt's own verdict."""
        if await lane.open():
            return lane
        call.absorb(lane)
        await lane.produced.aclose()
        return None

    async def _race(self, call: _Call, lanes: list[_Lane]) -> _Lane | None:
        """Run distinct candidates at once and commit to the first that produces
        anything. A lane a real failure emptied is refilled from a model this call has
        not tried; every lane still live once a winner exists is superseded."""
        opened = list(lanes)
        live = list(lanes)
        width = len(lanes)
        for lane in live:
            lane.task = asyncio.create_task(lane.open())
        winner: _Lane | None = None
        try:
            while live and winner is None:
                await asyncio.wait(
                    [lane.task for lane in live if lane.task is not None],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for lane in [settled for settled in live if settled.done()]:
                    live.remove(lane)
                    if not lane.opened():
                        call.absorb(lane)
                        await lane.produced.aclose()
                    elif winner is None:
                        winner = lane
                    else:
                        self._supersede(call, lane)
                if winner is None and not call.expired:
                    live.extend(await self._refill(call, opened, width - len(live)))
        except BaseException:
            await self._abandon(call, live)
            raise
        for lane in live:
            self._supersede(call, lane)
        return winner

    async def _untried(
        self,
        call: _Call,
        tried: frozenset[str],
        needed: int,
        eligible_names: frozenset[str] | None = None,
    ) -> list[LLMConfig]:
        """Whatever is free this instant among the models this call has not tried. Never
        waits: the lanes still racing must not be stalled to widen the field."""
        if needed <= 0:
            return []
        configs = await self._pool.take_free(
            payable=await self._payable(call.ring),
            width=needed,
            operation=call.operation,
            exclude=tried
            | call.client_failed
            | (call.attempted if eligible_names is not None else set()),
            answer_deadline=call.answer_deadline,
            eligible_names=eligible_names,
        )
        if eligible_names is not None:
            call.attempted.update(config.name for config in configs)
        return configs

    async def _refill(self, call: _Call, opened: list[_Lane], needed: int) -> list[_Lane]:
        """Reopen an emptied lane on a model this call has not tried."""
        configs = await self._untried(
            call,
            frozenset(lane.config.name for lane in opened),
            needed,
        )
        fresh = [self._open(call, config) for config in configs]
        for lane in fresh:
            lane.task = asyncio.create_task(lane.open())
        opened.extend(fresh)
        return fresh

    async def _abandon(self, call: _Call, live: list[_Lane]) -> None:
        """Nothing answered, so nothing was superseded: every lane is taken off its
        provider first and only then waited on, and whatever was already settling
        beside them is finished too."""
        for lane in live:
            self._cancel(lane)
        for lane in live:
            await self._stop(lane)
        await self._settled(call)

    def _supersede(self, call: _Call, lane: "_Lane | _StreamLane") -> None:
        """Settle a lane another model has already answered past: cancelled at once, but
        journaled and released beside the answer rather than in front of it — what the
        caller is holding may not wait on a store write it will never read."""
        lane.outcome.superseded = True
        self._cancel(lane)
        call.losers.append(asyncio.create_task(self._stop(lane)))

    @staticmethod
    def _cancel(lane: "_Lane | _StreamLane") -> None:
        """Take a lane off the provider. One already settling is left to finish: it has
        applied its own verdict to the pool and owes the journal the row for it."""
        if lane.task is not None and not lane.outcome.settling:
            lane.task.cancel()

    async def _settled(self, call: _Call) -> None:
        """Wait for the lanes settling beside the answer, so every attempt this call
        made is journaled and every slot handed back by the time it ends."""
        if not call.losers:
            return
        pending, call.losers = call.losers, []
        for outcome in await asyncio.gather(*pending, return_exceptions=True):
            if isinstance(outcome, Exception):
                logger.warning("llmbroker: settling a superseded attempt failed: %r", outcome)

    async def _stop(self, lane: "_Lane | _StreamLane") -> None:
        """Let a lane finish whatever it was doing and close it. Cancelling it is the
        caller's to do first, so a lane already settling is never cut short; one cut
        before its attempt began ran no code at all, so its slot is handed back here."""
        task = lane.task
        if task is not None:
            await asyncio.wait([task])
            if task.cancelled():
                if not lane.outcome.holds_slot:
                    await self._pool.release(lane.config)
            elif task.exception() is not None:
                logger.warning(
                    "llmbroker: the cancelled attempt on %s failed: %r",
                    lane.config.name,
                    task.exception(),
                )
        await lane.produced.aclose()

    @staticmethod
    def _publish(receipt: CallReceipt | None, winner: "_Lane | _StreamLane") -> None:
        """Name the winner on the handle the caller holds. Each lane names itself on
        its own until it has won, so a loser can never claim the answer."""
        if receipt is None:
            return
        won = winner.outcome.receipt
        receipt.llm_name = won.llm_name
        receipt.call_id = won.call_id
        receipt.usage = won.usage
        receipt.settled = won.settled

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
        outcome: _Outcome,
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
        outcome.holds_slot = True
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
        back, then journal it. The single failure surface both routing paths use.
        The two facts are independent — a missed budget may also be a cooldown."""
        delay: float | None = None
        budget_ms: int | None = None
        if verdict.http_status is not None and is_auth_failure(verdict.http_status):
            attempt.ring.forget(attempt.config.api_key_ref)
        if isinstance(verdict.outcome, _BudgetExpired):
            # Applied as well as journaled: the next caller on this node must not
            # wait on a rebuild, nor on learning being switched on.
            budget_ms = int(timeout * 1000)
            self._pool.raise_budget_bound(attempt.config.name, timeout, datetime.now(UTC))
        if verdict.cool_base is None:
            await self._pool.release(attempt.config)
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

    async def _settle_superseded(self, attempt: _Attempt, usage: Usage | None) -> None:
        """Settle a lane another model answered past: the slot goes back and one neutral
        row is written. Nothing is cooled, counted, bounded or rated — losing a race
        proves neither availability nor failure."""
        await self._pool.release(attempt.config)
        await self._record(attempt, CallStatus.SUPERSEDED, usage=usage)

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
        response_format: dict | None = None,
    ) -> AsyncGenerator[AsyncResult, None]:
        """Run one LLM and yield its single result, or leave on ``outcome`` the verdict
        the driver fails over on."""
        attempt = await self._new_attempt(
            config,
            ring,
            outcome,
            operation=operation,
            trace_id=trace_id,
        )
        if attempt is None:
            outcome.verdict = _Failed(error=None)
            return
        backoff = self._backoff(config.name)

        timeout, budget_bound = self._attempt_timeout(answer_deadline)
        if budget_bound and timeout == 0.0:
            outcome.settling = True
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
                    params=_request_params(response_format),
                )
        except _FAILOVER_ERRORS as exc:
            outcome.settling = True
            verdict = _classify(exc, budget_bound=budget_bound)
        except BaseException as exc:
            # A bug, a cancellation, or a sibling that answered first: settling before the
            # awaits, or a race cancels this one mid-release and costs a unit of `parallel`.
            outcome.settling = True
            if outcome.superseded:
                await self._settle_superseded(attempt, None)
            else:
                await self._pool.release(config)
                if isinstance(exc, Exception):
                    await self._record(attempt, CallStatus.ERROR, error_detail=type(exc).__name__)
            raise
        else:
            outcome.settling = True
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

    def _stream_backend(self) -> StreamBackend:
        """Expose the small service boundary used by the streaming modules."""
        return StreamBackend(
            configs=lambda: self._pool.configs,
            payable=self._payable,
            acquire=self._acquire,
            produce=self._produce,
            untried=self._untried,
            publish=self._publish,
            cancel=self._cancel,
            stop=self._stop,
            settled=self._settled,
            new_attempt=self._new_attempt,
            backoff=self._backoff,
            attempt_timeout=self._attempt_timeout,
            spent_budget=self._spent_budget,
            settle_superseded=self._settle_superseded,
            finish_ok=self._finish_ok,
            release=self._pool.release,
            record=self._record,
            dispose=self._dispose,
            http=lambda: self.http,
            store=self._store,
            observe_quality=(
                self._learner.record_quality_observed if self._learner is not None else None
            ),
        )

    def stream(  # noqa: PLR0913 - the chat knobs plus the caller's receipt
        self,
        ring: KeyRing,
        messages: list[dict],
        receipt: CallReceipt,
        *,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
        fastest_of: int | None = None,
        parallel_recovery: bool = True,
        response_format: dict | None = None,
        stream_selection_window: float = 1.0,
        _on_exhausted: Callable[[NoLLMAvailableError], Awaitable[bool]] | None = None,
    ) -> "_RoutedStream":
        """Return the lazy owner of one routed stream and its complete alternatives."""
        _check_lanes(fastest_of, parallel_recovery)
        _check_window(stream_selection_window)
        return _RoutedStream(
            self._stream_backend(),
            ring,
            messages,
            receipt,
            operation=operation,
            trace_id=trace_id,
            wait=wait,
            fastest_of=fastest_of,
            parallel_recovery=parallel_recovery,
            response_format=response_format,
            stream_selection_window=stream_selection_window,
            on_exhausted=_on_exhausted,
        )

    _drain_lane = staticmethod(_drain_stream_lane)

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
