"""Router: route one completion over the pool with per-LLM failover, journaling
every attempt. How each failure is disposed of is the contract in call-path.md."""

import asyncio
import logging
import math
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, aclosing, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, NoReturn, TypeVar, cast

import httpx

from llmbroker import chat
from llmbroker.broker.keyring import KeyRing
from llmbroker.broker.learning import Learner
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.result import AsyncResult, CallReceipt
from llmbroker.chat import (
    NO_DELTA,
    aiter_chat_chunks,
    build_chat_request,
    call_provider,
    empty_answer_error,
    parse_stream_chunk,
    provider_error,
    retry_after_seconds,
)
from llmbroker.exceptions import (
    InvalidProviderResponseError,
    LLMTimeoutError,
    NoLLMAvailableError,
    ProviderError,
    StreamInterruptedError,
    StreamReplacementError,
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

    error: ProviderError | None


class _BudgetExhaustedError(Exception):
    """The caller's own budget ran out with the answer still arriving. Private and raised
    only where the clock is: a ``TimeoutError`` here would classify as a provider failure
    and cool a model that answered."""


@dataclass(frozen=True)
class _BudgetExpired:
    """The caller's own ``wait`` ran out mid-attempt: the budget is journaled and
    teaches ordering, and the call ends on the caller's timeout. Whether the model is
    also cooled is ``cool_base``'s to say, independently of this."""


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
    """What one attempt reports back to the failover driver, and where it names what
    answered it. Its slot is settled and its row journaled by the time a verdict is
    set; ``receipt`` is the lane's own until the driver publishes the winner's."""

    receipt: CallReceipt = field(default_factory=CallReceipt)
    answered: bool = False
    verdict: "_Failed | _BudgetExpired | None" = None
    superseded: bool = False
    # The host stopped the lane it was reading: an answered attempt, not a failure.
    stopped: bool = False
    # Taken at provider completion, before the row is written: a slow store may not
    # turn the second provider to finish into the first.
    completed_at: float | None = None
    # Run where that instant is taken, before the row is written: a driver suspended on
    # a yield cannot act on it, and what it owes the losing lanes may not wait for a read.
    completed: "Callable[[], None] | None" = None
    # Set once the attempt is off the provider and settling itself: cancelling it there
    # would strand the row and the slot it still owes, whatever verdict it reached.
    settling: bool = False
    # Whether the attempt took charge of the slot acquired for it. A lane cancelled
    # before that ran none of its own code, so the driver hands the slot back for it.
    holds_slot: bool = False
    # A bug, which no verdict covers and nothing was learned from: retrying the same
    # candidate would only repeat it, so it leaves through the caller instead.
    crashed: Exception | None = None


@dataclass(slots=True)
class _Lane:
    """One model's attempt inside a call: what drives it, what it reported, and the
    task holding its first item until the driver decides who won."""

    config: LLMConfig
    outcome: _Outcome
    produced: AsyncIterator[Any]
    task: "asyncio.Task[bool] | None" = None
    first: Any = None

    async def open(self) -> bool:
        """Drive the attempt to its first item and hold it. ``False`` where the attempt
        failed instead — it has settled its own slot and journaled its own row by then."""
        try:
            self.first = await anext(self.produced)
        except StopAsyncIteration:
            return False
        return True

    def done(self) -> bool:
        return self.task is not None and self.task.done()

    def opened(self) -> bool:
        """Whether this lane produced an item; a bug it hit is re-raised here instead."""
        return self.task is not None and self.task.result()


@dataclass(slots=True)
class _Call:
    """One routed call's whole context: what each of its attempts runs, how many it may
    run at once, and what the attempts already settled have taught the driver."""

    attempt: Callable[..., AsyncIterator[Any]]
    ring: KeyRing
    operation: str | None
    trace_id: str | None
    answer_deadline: float | None
    width: int
    recovery_width: int
    client_failed: set[str] = field(default_factory=set)
    last_client_error: ProviderError | None = None
    expired: bool = False
    losers: list["asyncio.Task[None]"] = field(default_factory=list)

    def absorb(self, lane: "_Lane | _StreamLane") -> None:
        """Fold one failed attempt's verdict in, so every later candidate of this call
        is chosen against everything it has learned."""
        verdict = lane.outcome.verdict
        if isinstance(verdict, _BudgetExpired):
            self.expired = True
        elif isinstance(verdict, _Failed):
            self.client_failed.add(lane.config.name)
            if verdict.error is not None:
                self.last_client_error = verdict.error


@dataclass(slots=True)
class _StreamLane:
    """One model's streaming attempt inside an explicit race: everything it has produced,
    how much of that the caller has been given, and when it opened."""

    config: LLMConfig
    outcome: _Outcome
    produced: AsyncIterator[str]
    task: "asyncio.Task[None] | None" = None
    deltas: list[str] = field(default_factory=list)
    sent: int = 0
    first_delta_at: float | None = None
    failure: BaseException | None = None
    finished: bool = False
    absorbed: bool = False
    retired: bool = False


@dataclass(slots=True)
class _StreamRace:
    """One explicit streaming race: its lanes, which of them the caller is reading, and
    how long the pool's own first choice keeps its claim on being that one."""

    lanes: list[_StreamLane]
    width: int
    deadline: float = 0.0
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    exposed: _StreamLane | None = None
    done: bool = False

    @property
    def preferred(self) -> _StreamLane:
        return self.lanes[0]

    def live(self) -> list[_StreamLane]:
        return [lane for lane in self.lanes if not lane.finished]

    def winner(self) -> _StreamLane | None:
        """The lane whose provider completed a valid answer first."""
        best: _StreamLane | None = None
        best_at = 0.0
        for lane in self.lanes:
            at = lane.outcome.completed_at
            if at is not None and (best is None or at < best_at):
                best, best_at = lane, at
        return best

    def crashed(self) -> Exception | None:
        """A bug a lane hit, held like any other lane failure: nothing classified it, so
        it reaches the caller only once no lane is left that could answer."""
        return next((lane.outcome.crashed for lane in self.lanes if lane.outcome.crashed), None)

    def select(self) -> _StreamLane | None:
        """Whose deltas become the provisional stream, or ``None`` while the pool's first
        choice still holds its window and may yet begin."""
        preferred = self.preferred
        # Against the delta's own instant, never against now: the coordinator may reach
        # this a scheduling tick late, and a claim would not outlive its window for that.
        if preferred.first_delta_at is not None and preferred.first_delta_at <= self.deadline:
            return preferred
        if not preferred.finished and time.monotonic() < self.deadline:
            return None
        best: _StreamLane | None = None
        best_at = 0.0
        for lane in self.lanes:
            at = lane.first_delta_at
            if at is not None and (best is None or at < best_at):
                best, best_at = lane, at
        return best

    def timeout(self) -> float | None:
        """How long the coordinator may sleep — ``None`` where the window no longer
        decides anything and only a lane reporting can move the race on."""
        if self.exposed is not None or self.preferred.finished:
            return None
        remaining = self.deadline - time.monotonic()
        return remaining if remaining > 0 else None


def _check_lanes(fastest_of: int | None, parallel_recovery: bool) -> None:
    """Refuse both parallel options before any provider request opens."""
    if isinstance(fastest_of, bool) or (
        fastest_of is not None and (not isinstance(fastest_of, int) or fastest_of < 1)
    ):
        raise ValueError(f"fastest_of must be None or a positive int, got {fastest_of!r}")
    if parallel_recovery is not True and parallel_recovery is not False:
        raise ValueError(f"parallel_recovery must be True or False, got {parallel_recovery!r}")


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

    def settle(self) -> None:
        """Called once this attempt's journal row is written: a consumer that stopped
        before the first delta never reached ``opened``, and a rating may not precede the
        row it names."""
        self.receipt.llm_name = self.llm_name
        self.receipt.call_id = self.call_id
        self.receipt.usage = self.usage
        self.receipt.settled = True


async def _stream_deltas(
    client: httpx.AsyncClient,
    request: tuple[str, dict[str, str], dict],
    *,
    model: str,
    timeout: float,
    progress: _StreamProgress,
) -> AsyncIterator[str]:
    """Open one streaming request and yield its text deltas, recording progress.
    ``timeout`` bounds the whole answer in provider time: every provider await carries
    the same deadline, pushed on by whatever the consumer held between deltas."""
    url, headers, body = request
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        # Armed per await, not once around the answer: a timeout cancels the task that
        # entered it, and a raced answer changes hands once its first delta lands.
        async with AsyncExitStack() as opened:
            async with asyncio.timeout_at(deadline):
                resp = await opened.enter_async_context(
                    client.stream("POST", url, headers=headers, json=body),
                )
                if resp.status_code >= ERROR_FLOOR:
                    await resp.aread()
                    resp.raise_for_status()
            chunks = await opened.enter_async_context(aclosing(aiter_chat_chunks(resp, model)))
            while True:
                async with asyncio.timeout_at(deadline):
                    try:
                        chunk = await anext(chunks)
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
                deadline += loop.time() - held
    except TimeoutError:
        # Only the raise site knows the answer had started, and past the first delta a
        # timeout is the caller's budget rather than a provider failure.
        if progress.started:
            raise _BudgetExhaustedError from None
        raise


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
        # Mapped here, not at the re-raise: what a caller finally sees is llmbroker's
        # own provider error, never the transport library's exception type.
        return _Verdict(
            CallStatus.ERROR,
            detail,
            http_status=code,
            outcome=_Failed(error=provider_error(code, detail, exc.response.headers)),
        )
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
            cool_base=_DEFAULT_RATE_LIMIT_SEC,
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
        attempt: Callable[..., AsyncIterator[_Produced]],
        *,
        ring: KeyRing,
        operation: str | None,
        trace_id: str | None,
        wait: float | None,
        timeout_message: str,
        fastest_of: int | None = None,
        parallel_recovery: bool = True,
        receipt: CallReceipt | None = None,
    ) -> AsyncIterator[_Produced]:
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
    ) -> list[LLMConfig]:
        """Reserve this call's next candidates, or raise what the caller can act on."""
        try:
            return await self._pool.acquire_many(
                queue_deadline,
                payable=payable,
                width=call.width,
                recovery_width=call.recovery_width,
                operation=call.operation,
                exclude=frozenset(call.client_failed),
                answer_deadline=call.answer_deadline,
            )
        except NoLLMAvailableError as exc:
            if exc.reason == "excluded" and call.last_client_error is not None:
                raise call.last_client_error from None
            raise

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

    def _produce(self, call: _Call, config: LLMConfig, outcome: _Outcome) -> AsyncIterator[Any]:
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

    async def _untried(self, call: _Call, tried: frozenset[str], needed: int) -> list[LLMConfig]:
        """Whatever is free this instant among the models this call has not tried. Never
        waits: the lanes still racing must not be stalled to widen the field."""
        if needed <= 0:
            return []
        return await self._pool.take_free(
            payable=await self._payable(call.ring),
            width=needed,
            operation=call.operation,
            exclude=tried | call.client_failed,
            answer_deadline=call.answer_deadline,
        )

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
    ) -> AsyncIterator[AsyncResult]:
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

    async def stream(  # noqa: PLR0913 - the chat knobs plus the caller's receipt
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
    ) -> AsyncIterator[str]:
        """Route a streaming completion over the pool, yielding text deltas and naming
        what answered on ``receipt``. An explicit ``fastest_of`` above one races complete
        answers and may end in ``StreamReplacementError``; see call-path.md."""
        _check_lanes(fastest_of, parallel_recovery)
        _check_window(stream_selection_window)
        attempt = partial(
            self._stream_attempt,
            messages=messages,
            response_format=response_format,
        )
        shared: dict[str, Any] = {
            "ring": ring,
            "operation": operation,
            "trace_id": trace_id,
            "wait": wait,
            "timeout_message": "the wait budget ran out before any LLM produced a delta",
            "parallel_recovery": parallel_recovery,
            "receipt": receipt,
        }
        routed = (
            self._stream_route(
                attempt,
                fastest_of=fastest_of,
                window=stream_selection_window,
                **shared,
            )
            if fastest_of is not None and fastest_of > 1
            else self._route(attempt, fastest_of=fastest_of, **shared)
        )
        async with aclosing(routed) as deltas:
            async for delta in deltas:
                yield delta

    async def _stream_route(  # noqa: PLR0913 - one raced stream's whole context
        self,
        attempt: Callable[..., AsyncIterator[str]],
        *,
        ring: KeyRing,
        operation: str | None,
        trace_id: str | None,
        wait: float | None,
        timeout_message: str,
        fastest_of: int,
        parallel_recovery: bool,
        receipt: CallReceipt,
        window: float,
    ) -> AsyncIterator[str]:
        """Route a stream the caller asked to race: every lane runs on to a complete
        answer, and the first complete answer is what the call settles on."""
        queue_deadline = None if wait is None else time.monotonic() + wait
        answer_deadline = queue_deadline if wait else None
        call = _Call(
            attempt=attempt,
            ring=ring,
            operation=operation,
            trace_id=trace_id,
            answer_deadline=answer_deadline,
            width=fastest_of,
            recovery_width=2 if parallel_recovery else 1,
        )
        while True:
            payable = await self._payable(ring)
            configs = await self._acquire(call, queue_deadline, payable)
            race = self._start_race(call, configs, window)
            async with aclosing(self._run_race(call, race, receipt)) as deltas:
                async for delta in deltas:
                    yield delta
            if race.done:
                return
            if call.expired:
                self._expired(call, payable, timeout_message)
            # Every lane failed without answering ⇒ loop to the next free LLM.

    def _start_race(self, call: _Call, configs: list[LLMConfig], window: float) -> _StreamRace:
        """Open every lane at once and arm the selection window from that moment: it
        measures the pool's first choice against its siblings, not against the queue."""
        race = _StreamRace(lanes=[], width=len(configs))
        for config in configs:
            self._add_lane(race, call, config)
        race.deadline = time.monotonic() + window
        if call.answer_deadline is not None:
            race.deadline = min(race.deadline, call.answer_deadline)
        return race

    def _add_lane(self, race: _StreamRace, call: _Call, config: LLMConfig) -> None:
        outcome = _Outcome()
        lane = _StreamLane(
            config=config,
            outcome=outcome,
            produced=self._produce(call, config, outcome),
        )
        outcome.completed = partial(self._retire, call, race)
        race.lanes.append(lane)
        lane.task = asyncio.create_task(self._drain_lane(lane, race.wake))

    @staticmethod
    async def _drain_lane(lane: _StreamLane, wake: asyncio.Event) -> None:
        """Read one lane to its end whatever the caller is doing, so how fast the
        consumer pulls cannot decide which provider finished first."""
        try:
            async for delta in lane.produced:
                if lane.first_delta_at is None:
                    lane.first_delta_at = time.monotonic()
                lane.deltas.append(delta)
                wake.set()
        except Exception as exc:  # noqa: BLE001 - the coordinator decides what it means
            lane.failure = exc
        finally:
            lane.finished = True
            wake.set()

    async def _run_race(  # noqa: C901, PLR0912 - one loop is the coordinator
        self,
        call: _Call,
        race: _StreamRace,
        receipt: CallReceipt,
    ) -> AsyncIterator[str]:
        """Expose one lane's deltas provisionally while every lane runs on, and settle
        the call on the first complete answer — replacing the provisional text when the
        answer came from elsewhere."""
        try:
            while True:
                race.wake.clear()
                self._absorb_finished(call, race)
                winner = race.winner()
                if winner is not None:
                    replaced = race.exposed if race.exposed is not winner else None
                    if replaced is None:
                        if race.exposed is None:
                            race.exposed = winner
                            self._name(receipt, winner)
                        while winner.sent < len(winner.deltas):
                            winner.sent += 1
                            yield winner.deltas[winner.sent - 1]
                    if not winner.finished:
                        await self._pause(race)
                        continue
                    race.done = True
                    self._publish(receipt, winner)
                    if replaced is None:
                        return
                    raise StreamReplacementError(
                        f"{replaced.config.name}: its provisional deltas lost the race to a"
                        f" complete answer from {winner.config.name} — discard them and use"
                        " the replacement",
                        replacement=self._replacement(call, winner),
                        streamed_llm_name=replaced.config.name,
                    )
                if race.exposed is None:
                    race.exposed = race.select()
                    if race.exposed is not None:
                        self._name(receipt, race.exposed)
                exposed = race.exposed
                if exposed is not None and exposed.sent < len(exposed.deltas):
                    exposed.sent += 1
                    yield exposed.deltas[exposed.sent - 1]
                    continue
                if not race.live():
                    held = exposed.failure if exposed is not None else None
                    if held is None:
                        held = race.crashed()
                    if held is not None:
                        raise held
                    return
                if await self._refill_race(call, race):
                    continue
                await self._pause(race)
        finally:
            await self._close_race(call, race, receipt)

    @staticmethod
    def _absorb_finished(call: _Call, race: _StreamRace) -> None:
        """Fold each lane that ended without answering into the call, so every later
        candidate is chosen against everything this call has learned."""
        for lane in race.lanes:
            if lane.finished and not lane.absorbed:
                lane.absorbed = True
                if lane.outcome.completed_at is None:
                    call.absorb(lane)

    @staticmethod
    async def _pause(race: _StreamRace) -> None:
        """Sleep until a lane reports, or until the pool's first choice runs out of
        window — whichever comes first."""
        with suppress(TimeoutError):
            await asyncio.wait_for(race.wake.wait(), race.timeout())

    async def _refill_race(self, call: _Call, race: _StreamRace) -> bool:
        """Top the race back up from a model this call has not tried, exactly as an
        atomic race does when a real failure empties a lane."""
        if call.expired:
            return False
        configs = await self._untried(
            call,
            frozenset(lane.config.name for lane in race.lanes),
            race.width - len(race.live()),
        )
        for config in configs:
            self._add_lane(race, call, config)
        return bool(configs)

    def _retire(self, call: _Call, race: _StreamRace) -> None:
        """Take every other lane off its provider the instant a complete answer exists —
        a lane reporting is what does that, never the caller reading. Their rows and
        slots settle beside that answer, never in front of it."""
        winner = race.winner()
        if winner is None:
            return
        for lane in race.lanes:
            if lane is winner or lane.retired:
                continue
            lane.retired = True
            if not lane.finished:
                lane.outcome.superseded = True
                self._cancel(lane)
            call.losers.append(asyncio.create_task(self._stop(lane)))

    async def _close_race(self, call: _Call, race: _StreamRace, receipt: CallReceipt) -> None:
        """End every lane the race still owns. A lane that completed is the call whatever
        the host did next; where none did, the lane being read is the one it chose to stop,
        so that is what settles as answered."""
        settled = race.winner() or race.exposed
        for lane in race.lanes:
            if lane.retired:
                continue
            lane.retired = True
            if not lane.finished:
                if not race.done and lane is settled:
                    lane.outcome.stopped = True
                else:
                    lane.outcome.superseded = True
                self._cancel(lane)
            call.losers.append(asyncio.create_task(self._stop(lane)))
        await self._settled(call)
        if not race.done and settled is not None:
            self._publish(receipt, settled)

    @staticmethod
    def _name(receipt: CallReceipt, lane: _StreamLane) -> None:
        """Name the lane the caller is reading without settling the handle: provisional
        output may still be replaced, and a rating may not precede the answer."""
        receipt.llm_name = lane.outcome.receipt.llm_name
        receipt.call_id = lane.outcome.receipt.call_id

    def _replacement(self, call: _Call, winner: _StreamLane) -> AsyncResult:
        """The authoritative answer as a completed result — everything an ordinary routed
        call hands back, naming the lane that actually won."""
        return AsyncResult(
            text="".join(winner.deltas),
            tool_calls=None,
            usage=winner.outcome.receipt.usage,
            call_id=cast(str, winner.outcome.receipt.call_id),
            llm_name=winner.config.name,
            operation=call.operation,
            store=self._store,
            scope=call.ring.scope,
            observe_quality=(
                self._learner.record_quality_observed if self._learner is not None else None
            ),
        )

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
        response_format: dict | None = None,
    ) -> AsyncIterator[str]:
        """Stream one LLM, yielding its deltas. Leaves a verdict on ``outcome`` when it
        died before the first delta; the slot is settled and journaled by then."""
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
        timeout, budget_bound = self._attempt_timeout(first_delta_deadline)
        if budget_bound and timeout == 0.0:
            outcome.settling = True
            await self._spent_budget(attempt, outcome)
            return

        request = build_chat_request(
            config.base_url,
            config.model,
            attempt.resolved_key,
            messages,
            stream=True,
            params=_request_params(response_format),
        )
        progress = _StreamProgress(outcome.receipt, config.name, attempt.call_id)
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
        except _BudgetExhaustedError:
            outcome.settling = True
            await self._exhausted(attempt, timeout)
        except _FAILOVER_ERRORS as exc:
            outcome.settling = True
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
            await self._stream_stopped(attempt, progress, outcome)
            raise
        except BaseException as exc:
            outcome.settling = True
            await self._stream_aborted(attempt, progress, outcome, exc)
            raise
        else:
            outcome.settling = True
            await self._settle_stream(
                attempt,
                progress,
                outcome,
                backoff=backoff,
                timeout=timeout,
            )

    async def _stream_stopped(
        self,
        attempt: _Attempt,
        progress: _StreamProgress,
        outcome: _Outcome,
    ) -> None:
        """The consumer stopped pulling, or a sibling answered first: the model answered
        and did nothing wrong either way, so this is a completed attempt rather than a
        failure the pool should learn from."""
        if outcome.superseded:
            await self._settle_superseded(attempt, progress.usage)
            return
        await self._finish_ok(attempt, progress.usage)
        progress.settle()

    async def _stream_aborted(
        self,
        attempt: _Attempt,
        progress: _StreamProgress,
        outcome: _Outcome,
        exc: BaseException,
    ) -> None:
        """A bug, or a cancellation: the slot goes back whatever happened, and only what
        the attempt itself did is journaled."""
        if outcome.superseded or outcome.stopped:
            await self._stream_stopped(attempt, progress, outcome)
            return
        await self._pool.release(attempt.config)
        if isinstance(exc, Exception):
            outcome.crashed = exc
            await self._record(attempt, CallStatus.ERROR, error_detail=type(exc).__name__)

    async def _settle_stream(
        self,
        attempt: _Attempt,
        progress: _StreamProgress,
        outcome: _Outcome,
        *,
        backoff: float,
        timeout: float,
    ) -> None:
        """Settle a streaming attempt whose deltas ran out. One that never produced a
        delta answered nothing, so it fails over through the same surface a malformed
        body does — classified there, so there is one reading of an unusable 200."""
        if not progress.started:
            verdict = _classify(
                empty_answer_error(attempt.config.name, NO_DELTA),
                budget_bound=False,
            )
            await self._dispose(attempt, verdict, backoff=backoff, timeout=timeout)
            outcome.verdict = verdict.outcome
            return
        outcome.completed_at = time.monotonic()
        if outcome.completed is not None:
            outcome.completed()
        await self._finish_ok(attempt, progress.usage)
        progress.settle()
        outcome.answered = True

    async def _exhausted(self, attempt: _Attempt, timeout: float) -> NoReturn:
        """Settle a stream that outlived the caller's budget: the model answered and did
        nothing wrong, so it is journaled as a budget it did not finish within rather than
        cooled, and the call ends by raising — nothing is retried past the first delta."""
        elapsed = time.monotonic() - attempt.t0
        await self._dispose(
            attempt,
            _Verdict(
                CallStatus.ERROR,
                f"answer budget exhausted after {elapsed:.1f}s",
                outcome=_BudgetExpired(),
            ),
            backoff=self._backoff(attempt.config.name),
            # The bound this teaches is provider time, which the wall clock overstates
            # by whatever the consumer held between deltas.
            timeout=timeout,
        )
        raise LLMTimeoutError(
            f"{attempt.config.name}: the answer was still arriving {elapsed:.1f}s in, past"
            " the budget, and nothing can be retried once output has reached the caller",
        )

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
