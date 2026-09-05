"""Router: route one completion over the pool with per-LLM failover, journaling
every attempt. How each failure is disposed of is the contract in call-path.md."""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, aclosing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, NoReturn, TypeVar

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
    # Set once the attempt is off the provider and settling itself: cancelling it there
    # would strand the row and the slot it still owes, whatever verdict it reached.
    settling: bool = False


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

    def absorb(self, lane: _Lane) -> None:
        """Fold one failed attempt's verdict in, so every later candidate of this call
        is chosen against everything it has learned."""
        verdict = lane.outcome.verdict
        if isinstance(verdict, _BudgetExpired):
            self.expired = True
        elif isinstance(verdict, _Failed):
            self.client_failed.add(lane.config.name)
            if verdict.error is not None:
                self.last_client_error = verdict.error


def _check_lanes(fastest_of: int | None, parallel_recovery: bool) -> None:
    """Refuse both parallel options before any provider request opens."""
    if isinstance(fastest_of, bool) or (
        fastest_of is not None and (not isinstance(fastest_of, int) or fastest_of < 1)
    ):
        raise ValueError(f"fastest_of must be None or a positive int, got {fastest_of!r}")
    if parallel_recovery is not True and parallel_recovery is not False:
        raise ValueError(f"parallel_recovery must be True or False, got {parallel_recovery!r}")


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
    ) -> AsyncResult:
        return await self.chat(
            ring,
            [{"role": "user", "content": prompt}],
            operation=operation,
            trace_id=trace_id,
            wait=wait,
            fastest_of=fastest_of,
            parallel_recovery=parallel_recovery,
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
    ) -> AsyncResult:
        _check_lanes(fastest_of, parallel_recovery)
        routed = self._route(
            partial(self._attempt, messages=messages, tools=tools),
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
            payable = await ring.payable(c.api_key_ref for c in self._pool.configs.values())
            try:
                configs = await self._pool.acquire_many(
                    queue_deadline,
                    payable=payable,
                    width=call.width,
                    recovery_width=call.recovery_width,
                    operation=operation,
                    exclude=frozenset(call.client_failed),
                    answer_deadline=answer_deadline,
                )
            except NoLLMAvailableError as exc:
                if exc.reason == "excluded" and call.last_client_error is not None:
                    raise call.last_client_error from None
                raise

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
                # A request an earlier LLM already rejected as malformed stays the
                # more useful answer than "the clock ran out" — the caller can act on it.
                if call.last_client_error is not None:
                    raise call.last_client_error from None
                raise NoLLMAvailableError(
                    timeout_message,
                    reason="timeout",
                    retry_at=self._pool.retry_at(payable, exclude=frozenset(call.client_failed)),
                )
            # Every lane failed without answering ⇒ loop to the next free LLM.

    def _open(self, call: _Call, config: LLMConfig) -> _Lane:
        """One candidate's lane, not started until something drives it."""
        outcome = _Outcome()
        return _Lane(
            config=config,
            outcome=outcome,
            produced=call.attempt(
                config,
                outcome,
                call.answer_deadline,
                ring=call.ring,
                operation=call.operation,
                trace_id=call.trace_id,
            ),
        )

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

    async def _refill(self, call: _Call, opened: list[_Lane], needed: int) -> list[_Lane]:
        """Reopen an emptied lane on a model this call has not tried. Never waits: the
        lanes still racing must not be stalled to widen the field."""
        if needed <= 0:
            return []
        payable = await call.ring.payable(c.api_key_ref for c in self._pool.configs.values())
        configs = await self._pool.take_free(
            payable=payable,
            width=needed,
            operation=call.operation,
            exclude=frozenset({lane.config.name for lane in opened}) | call.client_failed,
            answer_deadline=call.answer_deadline,
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

    def _supersede(self, call: _Call, lane: _Lane) -> None:
        """Settle a lane another model has already answered past: cancelled at once, but
        journaled and released beside the answer rather than in front of it — what the
        caller is holding may not wait on a store write it will never read."""
        lane.outcome.superseded = True
        self._cancel(lane)
        call.losers.append(asyncio.create_task(self._stop(lane)))

    @staticmethod
    def _cancel(lane: _Lane) -> None:
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

    async def _stop(self, lane: _Lane) -> None:
        """Let a lane finish whatever it was doing and close it. Cancelling it is the
        caller's to do first, so a lane already settling is never cut short."""
        task = lane.task
        if task is not None:
            await asyncio.wait([task])
            if not task.cancelled() and task.exception() is not None:
                logger.warning(
                    "llmbroker: the cancelled attempt on %s failed: %r",
                    lane.config.name,
                    task.exception(),
                )
        await lane.produced.aclose()

    @staticmethod
    def _publish(receipt: CallReceipt | None, winner: _Lane) -> None:
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
    ) -> AsyncIterator[str]:
        """Route a streaming completion over the pool, yielding text deltas and naming
        what answered on ``receipt``. Fails over exactly like ``chat`` up to the first
        delta; past it a death raises ``StreamInterruptedError`` instead."""
        _check_lanes(fastest_of, parallel_recovery)
        routed = self._route(
            partial(self._stream_attempt, messages=messages),
            ring=ring,
            operation=operation,
            trace_id=trace_id,
            wait=wait,
            timeout_message="the wait budget ran out before any LLM produced a delta",
            fastest_of=fastest_of,
            parallel_recovery=parallel_recovery,
            receipt=receipt,
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
            outcome.settling = True
            await self._spent_budget(attempt, outcome)
            return

        request = build_chat_request(
            config.base_url,
            config.model,
            attempt.resolved_key,
            messages,
            stream=True,
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
        if outcome.superseded:
            await self._settle_superseded(attempt, progress.usage)
            return
        await self._pool.release(attempt.config)
        if isinstance(exc, Exception):
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
