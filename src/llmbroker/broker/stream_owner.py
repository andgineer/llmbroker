"""Lifetime owner for a routed stream and its retained alternative answers."""

import asyncio
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import suppress
from functools import partial
from typing import Any, NoReturn, cast

from llmbroker.broker.keyring import KeyRing
from llmbroker.broker.result import AsyncResult, CallReceipt
from llmbroker.broker.route_state import Outcome, RouteCall
from llmbroker.broker.streaming import (
    StreamBackend,
    StreamBudget,
    StreamLane,
    StreamRace,
    name,
    replacement,
    stream_attempt,
)
from llmbroker.exceptions import NoLLMAvailableError, StreamReplacementError
from llmbroker.models import LLMConfig


class RoutedStream:
    """Own one streaming route until its public handle is explicitly closed."""

    def __init__(  # noqa: PLR0913
        self,
        backend: StreamBackend,
        ring: KeyRing,
        messages: list[dict],
        receipt: CallReceipt,
        *,
        operation: str | None,
        trace_id: str | None,
        wait: float | None,
        fastest_of: int | None,
        parallel_recovery: bool,
        response_format: dict | None,
        stream_selection_window: float,
        on_exhausted: Callable[[NoLLMAvailableError], Awaitable[bool]] | None,
    ) -> None:
        self._backend = backend
        self._ring = ring
        self._messages = messages
        self._receipt = receipt
        self._operation = operation
        self._trace_id = trace_id
        self._wait = wait
        self._fastest_of = fastest_of
        self._parallel_recovery = parallel_recovery
        self._response_format = response_format
        self._window = stream_selection_window
        self._on_exhausted = on_exhausted
        self._call: RouteCall | None = None
        self._race: StreamRace | None = None
        self._budget: StreamBudget | None = None
        self._queue_deadline: float | None = None
        self._eligible_names: set[str] = set()
        self._refresh_used = False
        self._initial: AsyncGenerator[str, None] | None = None
        self._initial_complete = False
        self._authoritative: StreamLane | None = None
        self._delivered: set[str] = set()
        self._terminal = False
        self._closed = False
        self._active: asyncio.Task[Any] | None = None
        self._cleanup: asyncio.Task[None] | None = None
        self._opening_order = 0
        self._wave = -1
        self._wave_width: dict[int, int] = {}

    def __aiter__(self) -> "RoutedStream":
        return self

    async def __anext__(self) -> str:
        if self._closed:
            raise StopAsyncIteration
        if self._active is not None:
            raise RuntimeError("the stream already has an active pull or continuation")
        if self._initial is None:
            self._initial = self._initial_deltas()
        task = asyncio.create_task(self._pull_initial(self._initial))
        self._active = task
        try:
            return await task
        except StopAsyncIteration:
            if not self._initial_complete:
                await self._close_after_fault()
            raise
        except StreamReplacementError:
            raise
        except BaseException:
            await self._close_after_fault()
            raise
        finally:
            if self._active is task:
                self._active = None

    @staticmethod
    async def _pull_initial(initial: AsyncIterator[str]) -> str:
        return await anext(initial)

    async def another(self) -> AsyncResult | None:
        if self._closed:
            raise RuntimeError("the stream is closed")
        if not self._initial_complete:
            raise RuntimeError("another answer requires a complete initial answer")
        if self._active is not None:
            raise RuntimeError("the stream already has an active pull or continuation")
        if self._terminal:
            return None
        task = asyncio.create_task(self._next_answer())
        self._active = task
        try:
            return await task
        except BaseException:
            await self._close_after_fault()
            raise
        finally:
            if self._active is task:
                self._active = None

    async def aclose(self) -> None:
        if self._cleanup is None:
            self._closed = True
            self._cleanup = asyncio.create_task(self._close())
        await asyncio.shield(self._cleanup)

    async def _close_after_fault(self) -> None:
        if self._cleanup is None:
            self._closed = True
            self._active = None
            self._cleanup = asyncio.create_task(self._close())
        with suppress(asyncio.CancelledError):
            await asyncio.shield(self._cleanup)

    def _start(self) -> None:
        now = time.monotonic()
        self._queue_deadline = None if self._wait is None else now + self._wait
        answer_deadline = self._queue_deadline if self._wait else None
        self._budget = StreamBudget(answer_deadline)
        attempt = partial(
            stream_attempt,
            self._backend,
            messages=self._messages,
            response_format=self._response_format,
            budget=self._budget,
        )
        self._call = RouteCall(
            attempt=attempt,
            ring=self._ring,
            operation=self._operation,
            trace_id=self._trace_id,
            answer_deadline=answer_deadline,
            width=self._fastest_of if self._fastest_of is not None and self._fastest_of > 1 else 1,
            recovery_width=2 if self._parallel_recovery else 1,
        )
        self._race = StreamRace(lanes=[], width=self._call.width)
        self._eligible_names.update(self._backend.configs())

    @property
    def _current_call(self) -> RouteCall:
        return cast(RouteCall, self._call)

    @property
    def _current_race(self) -> StreamRace:
        return cast(StreamRace, self._race)

    async def _initial_deltas(self) -> AsyncGenerator[str, None]:
        self._start()
        if self._fastest_of is not None and self._fastest_of > 1:
            async for delta in self._explicit_initial():
                yield delta
            return
        async for delta in self._ordinary_initial():
            yield delta

    async def _acquire(self, *, initial: bool) -> list[LLMConfig]:
        call = self._current_call
        while True:
            payable = await self._backend.payable(self._ring)
            try:
                return await self._backend.acquire(
                    call,
                    self._queue_deadline,
                    payable,
                    frozenset(self._eligible_names),
                )
            except NoLLMAvailableError as exc:
                if (
                    initial
                    and not self._refresh_used
                    and self._on_exhausted is not None
                    and await self._on_exhausted(exc)
                ):
                    self._refresh_used = True
                    self._eligible_names.update(self._backend.configs())
                    continue
                raise

    def _open(
        self,
        configs: list[LLMConfig],
        *,
        hold_first: bool,
        new_wave: bool = False,
    ) -> None:
        call = self._current_call
        race = self._current_race
        if new_wave:
            self._wave += 1
            self._wave_width[self._wave] = max(call.width, len(configs))
        for config in configs:
            outcome = Outcome()
            lane = StreamLane(
                config=config,
                outcome=outcome,
                produced=self._backend.produce(call, config, outcome),
                opening_order=self._opening_order,
                hold_first=hold_first,
                wave=self._wave,
            )
            self._opening_order += 1
            race.lanes.append(lane)
            lane.task = asyncio.create_task(self._drain(lane))

    async def _drain(self, lane: StreamLane) -> None:
        race = self._current_race
        try:
            async for delta in lane.produced:
                if lane.first_delta_at is None:
                    lane.first_delta_at = time.monotonic()
                lane.deltas.append(delta)
                race.wake.set()
                if lane.hold_first and len(lane.deltas) == 1:
                    await lane.release_first.wait()
                if lane.consumer_driven:
                    await lane.pulled.wait()
                    lane.pulled.clear()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            lane.failure = exc
        finally:
            lane.finished = True
            race.wake.set()

    def _absorb_finished(self) -> None:
        call = self._current_call
        for lane in self._current_race.lanes:
            if lane.finished and not lane.absorbed and lane.outcome.completed_at is None:
                lane.absorbed = True
                call.absorb(lane)

    async def _pause(self) -> None:
        await self._current_race.wake.wait()

    def _resume_budget(self) -> None:
        elapsed = self._budget.resume() if self._budget is not None else 0.0
        call = self._current_call
        if call.answer_deadline is not None:
            call.answer_deadline += elapsed
        if self._queue_deadline is not None:
            self._queue_deadline += elapsed

    async def _yield_visible(self, lane: StreamLane) -> AsyncIterator[str]:
        race = self._current_race
        while True:
            race.wake.clear()
            if lane.sent < len(lane.deltas):
                delta = lane.deltas[lane.sent]
                lane.sent += 1
                if self._budget is not None:
                    self._budget.pause()
                yield delta
                self._resume_budget()
                lane.pulled.set()
                continue
            if lane.finished:
                return
            await self._refill_free()
            await race.wake.wait()

    def _first_delta(self) -> StreamLane | None:
        candidates = [lane for lane in self._current_race.lanes if lane.first_delta_at is not None]
        return min(
            candidates,
            key=lambda lane: (cast(float, lane.first_delta_at), lane.opening_order),
            default=None,
        )

    async def _ordinary_initial(self) -> AsyncIterator[str]:  # noqa: C901
        while self._authoritative is None:
            self._current_race.wake.clear()
            self._absorb_finished()
            if self._current_race.lanes:
                await self._refill_free()
            if not self._current_race.live():
                if self._current_call.expired:
                    self._raise_initial_fault(
                        NoLLMAvailableError(
                            "the wait budget ran out before any LLM produced a delta",
                            reason="timeout",
                        ),
                    )
                try:
                    self._open(await self._acquire(initial=True), hold_first=True, new_wave=True)
                except NoLLMAvailableError as exc:
                    self._raise_initial_fault(exc)
            selected = self._first_delta()
            if selected is None:
                if not self._current_race.live():
                    continue
                await self._pause()
                continue
            self._authoritative = selected
            self._current_race.exposed = selected
            selected.consumer_driven = True
            name(self._receipt, selected)
            for lane in self._current_race.lanes:
                lane.release_first.set()
        selected = self._authoritative
        async for delta in self._yield_visible(selected):
            await self._refill_free()
            yield delta
        if selected.failure is not None:
            raise selected.failure
        if not selected.outcome.answered:
            self._raise_initial_fault(
                NoLLMAvailableError("no LLM in the pool produced an answer", reason="excluded"),
            )
        self._complete_initial(selected)

    async def _explicit_initial(self) -> AsyncIterator[str]:  # noqa: C901, PLR0912
        race = self._current_race
        while True:
            race.wake.clear()
            self._absorb_finished()
            winner = race.winner()
            if winner is not None and winner.finished:
                replaced = race.exposed if race.exposed is not winner else None
                if replaced is None:
                    if race.exposed is None:
                        race.exposed = winner
                        name(self._receipt, winner)
                    while winner.sent < len(winner.deltas):
                        delta = winner.deltas[winner.sent]
                        winner.sent += 1
                        yield delta
                self._complete_initial(winner)
                if replaced is None:
                    return
                raise StreamReplacementError(
                    f"{replaced.config.name}: its provisional deltas lost the race to a"
                    f" complete answer from {winner.config.name} — discard them and use"
                    " the replacement",
                    replacement=replacement(self._backend, self._current_call, winner),
                    streamed_llm_name=replaced.config.name,
                )
            if not race.lanes or not race.live():
                if self._current_call.expired:
                    self._raise_initial_fault(
                        NoLLMAvailableError(
                            "the wait budget ran out before any LLM produced a delta",
                            reason="timeout",
                        ),
                    )
                try:
                    self._open(await self._acquire(initial=True), hold_first=False, new_wave=True)
                except NoLLMAvailableError as exc:
                    self._raise_initial_fault(exc)
                race.deadline = time.monotonic() + self._window
                if self._current_call.answer_deadline is not None:
                    race.deadline = min(race.deadline, self._current_call.answer_deadline)
                continue
            if race.exposed is None:
                race.exposed = race.select()
                if race.exposed is not None:
                    name(self._receipt, race.exposed)
            exposed = race.exposed
            if exposed is not None and exposed.sent < len(exposed.deltas):
                delta = exposed.deltas[exposed.sent]
                exposed.sent += 1
                yield delta
                continue
            await self._refill_free()
            await self._pause_explicit()

    async def _pause_explicit(self) -> None:
        race = self._current_race
        with suppress(TimeoutError):
            await asyncio.wait_for(race.wake.wait(), race.timeout())

    async def _refill_free(self) -> None:
        call = self._current_call
        race = self._current_race
        if call.expired or self._buffered_lane() is not None:
            return
        wave = self._wave
        delivered = sum(
            lane.outcome.receipt.call_id in self._delivered
            for lane in race.lanes
            if lane.wave == wave
        )
        pending = sum(
            (not lane.finished or lane.outcome.completed_at is not None)
            and lane.outcome.receipt.call_id not in self._delivered
            for lane in race.lanes
            if lane.wave == wave
        )
        target = max(self._wave_width.get(wave, call.width) - delivered, 0)
        needed = target - pending
        configs = await self._backend.untried(
            call,
            frozenset(),
            needed,
            frozenset(self._eligible_names),
            still_needed=lambda: self._buffered_lane() is None,
        )
        self._open(configs, hold_first=False)

    def _complete_initial(self, winner: StreamLane) -> None:
        self._authoritative = winner
        self._initial_complete = True
        self._current_race.done = True
        self._backend.publish(self._receipt, winner)
        if winner.outcome.receipt.call_id is not None:
            self._delivered.add(winner.outcome.receipt.call_id)

    def _buffered_lane(self) -> StreamLane | None:
        candidates = [
            lane
            for lane in self._current_race.lanes
            if lane.outcome.completed_at is not None
            and lane.outcome.receipt.call_id not in self._delivered
        ]
        return min(
            candidates,
            key=lambda lane: (cast(float, lane.outcome.completed_at), lane.opening_order),
            default=None,
        )

    async def _next_answer(self) -> AsyncResult | None:  # noqa: C901
        call = self._current_call
        race = self._current_race
        while True:
            race.wake.clear()
            self._absorb_finished()
            lane = self._buffered_lane()
            if lane is not None:
                if lane.task is not None and not lane.task.done():
                    await asyncio.shield(lane.task)
                call_id = lane.outcome.receipt.call_id
                if call_id is not None:
                    self._delivered.add(call_id)
                return replacement(self._backend, call, lane)
            await self._refill_free()
            lane = self._buffered_lane()
            if lane is not None:
                continue
            if race.live():
                await self._pause()
                continue
            unexpected = self._unexpected_fault()
            if call.expired or (
                self._budget is not None
                and self._budget.deadline is not None
                and time.monotonic() >= self._budget.deadline
            ):
                if call.last_client_error is not None:
                    raise call.last_client_error
                if unexpected is not None:
                    raise unexpected
                self._terminal = True
                return None
            try:
                configs = await self._acquire(initial=False)
            except NoLLMAvailableError:
                if call.last_client_error is not None:
                    raise call.last_client_error from None
                if unexpected is not None:
                    raise unexpected from None
                self._terminal = True
                return None
            self._open(configs, hold_first=False, new_wave=True)

    def _unexpected_fault(self) -> Exception | None:
        crashed = [
            (lane.opening_order, lane.outcome.crashed)
            for lane in self._current_race.lanes
            if lane.outcome.crashed is not None
        ]
        return min(crashed, default=(0, None))[1]

    def _raise_initial_fault(self, fallback: NoLLMAvailableError) -> NoReturn:
        exposed = self._current_race.exposed
        if exposed is not None and isinstance(exposed.failure, Exception):
            raise exposed.failure
        unexpected = self._unexpected_fault()
        if unexpected is not None:
            raise unexpected
        call = self._current_call
        if call.last_client_error is not None:
            raise call.last_client_error
        raise fallback

    async def _close(self) -> None:
        active = self._active
        if active is not None and not active.done():
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        initial = self._initial
        if initial is not None:
            with suppress(RuntimeError):
                await initial.aclose()
        race = self._race
        if race is None:
            return
        completed = race.winner()
        settled = completed or self._authoritative or race.exposed
        stops: list[Awaitable[None]] = []
        for lane in race.lanes:
            if lane.retired:
                continue
            lane.retired = True
            if not lane.finished:
                if not self._initial_complete and completed is None and lane is settled:
                    lane.outcome.stopped = True
                else:
                    lane.outcome.superseded = True
                self._backend.cancel(lane)
            stops.append(self._backend.stop(lane))
        if stops:
            await asyncio.gather(*stops, return_exceptions=True)
        await self._backend.settled(self._current_call)
        if not self._initial_complete and settled is not None:
            self._backend.publish(self._receipt, settled)
        self._race = None
        self._authoritative = None
        self._initial = None
