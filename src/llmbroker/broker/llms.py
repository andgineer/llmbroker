"""``AsyncLLMs``: one caller's view of the installation's pool — the scope its
journal rows carry and the keys it may pay with. Built by the broker only."""

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import aclosing
from datetime import UTC, datetime, timedelta

import httpx

from llmbroker.broker.catalog import Catalog, find_declared
from llmbroker.broker.keyring import KeyRing
from llmbroker.broker.learning import Learner
from llmbroker.broker.pool_view import PoolView
from llmbroker.broker.result import AsyncLLM, AsyncResult, CallReceipt, StreamHandle
from llmbroker.broker.router import Router
from llmbroker.broker.stats import stats_from_calls
from llmbroker.direct import AsyncDirectClient
from llmbroker.exceptions import MissingKeyError, NoLLMAvailableError, UnknownCallError
from llmbroker.models import (
    Call,
    CallStatus,
    LLMConfig,
    LLMStats,
    check_limit,
    check_score,
    to_utc,
)
from llmbroker.protocols.store import QueryableStoreProtocol, StoreProtocol

logger = logging.getLogger("llmbroker.broker")

_DEFAULT_STATS_LIMIT = 1000

# How far back a rating key is looked up: an older verdict could not outlive one
# rebuild of the quality window, so it does not earn a scan (rules/selection.md).
_RATING_WINDOW = timedelta(days=7)

# One call's attempts: enough that the answered row is in the page even after failover
# wrote several, small enough that a reused trace is a warning rather than a scan.
_RATING_PAGE = 20


class AsyncLLMs:
    """Route and rate calls over the broker's one shared pool, paying with this
    caller's keys and writing its scope on every row it journals."""

    def __init__(  # noqa: PLR0913 - a caller is its ring over the installation's parts
        self,
        ring: KeyRing,
        *,
        router: Router,
        catalog: Catalog,
        pool_view: PoolView,
        store: StoreProtocol,
        learner: Learner | None,
        ensure_pool: Callable[[], Awaitable[None]],
        on_exhausted: Callable[[NoLLMAvailableError, KeyRing], Awaitable[bool]],
    ) -> None:
        self._ring = ring
        self._router = router
        self._catalog = catalog
        self._pool_view = pool_view
        self._store = store
        self._learner = learner
        self._ensure_pool = ensure_pool
        self._on_exhausted = on_exhausted

    @property
    def scope(self) -> str | None:
        """Whose calls these are — the attribution on this caller's journal rows."""
        return self._ring.scope

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    async def ask(  # noqa: PLR0913 - the call knobs, one keyword each
        self,
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
            [{"role": "user", "content": prompt}],
            operation=operation,
            trace_id=trace_id,
            wait=wait,
            fastest_of=fastest_of,
            parallel_recovery=parallel_recovery,
            response_format=response_format,
        )

    async def chat(  # noqa: PLR0913 - what to send, and the call knobs
        self,
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
        await self._ensure_pool()
        try:
            return await self._router.chat(
                self._ring,
                messages,
                tools=tools,
                operation=operation,
                trace_id=trace_id,
                wait=wait,
                fastest_of=fastest_of,
                parallel_recovery=parallel_recovery,
                response_format=response_format,
            )
        except NoLLMAvailableError as exc:
            if not await self._on_exhausted(exc, self._ring):
                raise
        # The ports were re-read inside this request, so it may as well have the
        # answer. ``wait=0``: a second pass may not spend the budget twice.
        return await self._router.chat(
            self._ring,
            messages,
            tools=tools,
            operation=operation,
            trace_id=trace_id,
            wait=0,
            fastest_of=fastest_of,
            parallel_recovery=parallel_recovery,
            response_format=response_format,
        )

    def stream(  # noqa: PLR0913 - the call knobs, one keyword each
        self,
        prompt: str,
        *,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
        fastest_of: int | None = None,
        parallel_recovery: bool = True,
        response_format: dict | None = None,
        stream_selection_window: float = 1.0,
    ) -> StreamHandle:
        """Route a completion over the pool as a handle yielding text deltas and naming
        what answered them. ``wait`` bounds the whole answer in provider time; an explicit
        ``fastest_of`` above one may end in ``StreamReplacementError``. Async-only."""
        receipt = CallReceipt()
        return StreamHandle(
            self._deltas(
                receipt,
                prompt,
                operation=operation,
                trace_id=trace_id,
                wait=wait,
                fastest_of=fastest_of,
                parallel_recovery=parallel_recovery,
                response_format=response_format,
                stream_selection_window=stream_selection_window,
            ),
            receipt,
            operation=operation,
            store=self._store,
            scope=self.scope,
            observe_quality=(
                self._learner.record_quality_observed if self._learner is not None else None
            ),
        )

    async def _deltas(  # noqa: PLR0913 - the call knobs, one keyword each
        self,
        receipt: CallReceipt,
        prompt: str,
        *,
        operation: str | None,
        trace_id: str | None,
        wait: float | None,
        fastest_of: int | None,
        parallel_recovery: bool,
        response_format: dict | None,
        stream_selection_window: float,
    ) -> AsyncGenerator[str, None]:
        await self._ensure_pool()
        messages = [{"role": "user", "content": prompt}]
        produced = False
        try:
            async with aclosing(
                self._router.stream(
                    self._ring,
                    messages,
                    receipt,
                    operation=operation,
                    trace_id=trace_id,
                    wait=wait,
                    fastest_of=fastest_of,
                    parallel_recovery=parallel_recovery,
                    response_format=response_format,
                    stream_selection_window=stream_selection_window,
                ),
            ) as deltas:
                async for delta in deltas:
                    produced = True
                    yield delta
            return
        except NoLLMAvailableError as exc:
            # ``produced`` guards invariant 18: past the first delta the answer is
            # already partly the caller's, and a second pass could only splice.
            if produced or not await self._on_exhausted(exc, self._ring):
                raise
        async with aclosing(
            self._router.stream(
                self._ring,
                messages,
                receipt,
                operation=operation,
                trace_id=trace_id,
                wait=0,
                fastest_of=fastest_of,
                parallel_recovery=parallel_recovery,
                response_format=response_format,
                stream_selection_window=stream_selection_window,
            ),
        ) as deltas:
            async for delta in deltas:
                yield delta

    # ------------------------------------------------------------------
    # Direct single-model access (no pool, no failover)
    # ------------------------------------------------------------------

    async def direct(
        self,
        alias: str | None = None,
        *,
        name: str | None = None,
    ) -> AsyncDirectClient:
        """A client for exactly one model of your own — no pool, no failover.

        Takes exactly one of ``alias`` or ``name=``; raises ``PoolModelError``,
        ``UnknownModelError`` or ``MissingKeyError``. See ``docs/`` "Direct model calls".
        """
        cfg, key = await self.resolve_direct(alias, name=name)
        return AsyncDirectClient(
            base_url=cfg.base_url,
            model=cfg.model,
            api_key=key,
            client=self._http(),
        )

    async def resolve_direct(
        self,
        alias: str | None = None,
        *,
        name: str | None = None,
    ) -> tuple[LLMConfig, str]:
        """Look the entry up in its keyspace and resolve it against this caller's ring
        (shared by the synchronous façade)."""
        if (alias is None) == (name is None):
            raise ValueError(
                "direct() takes exactly one of alias (positional) or name= —"
                " they are separate keyspaces",
            )
        stored, declared = await self._catalog.entries()
        cfg = find_declared(stored, declared, alias, name)
        ref = alias if alias is not None else name
        key = await self._ring.resolve(cfg.api_key_ref)
        if key is None:
            hint = self._catalog.key_help(cfg.api_key_ref)
            raise MissingKeyError(
                f"api_key_ref {cfg.api_key_ref!r} for model {ref!r} could not be resolved"
                " — set the env var or configure a secrets backend" + (f". {hint}" if hint else ""),
            )
        return cfg, key

    def _http(self) -> httpx.AsyncClient:
        """The one client of the installation — a caller opens no connection pool of
        its own."""
        return self._router.http

    # ------------------------------------------------------------------
    # Inspection and rating
    # ------------------------------------------------------------------

    async def get(self, name: str) -> AsyncLLM:
        await self._ensure_pool()
        return self._pool_view.get(name)

    async def count(self) -> int:
        await self._ensure_pool()
        return self._pool_view.count()

    async def record_quality(
        self,
        score: float,
        *,
        call_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        """Rate a past call, by exactly one key — one rating, on the newest answered
        call the key names within the rating window. Raises ``UnknownCallError`` when
        nothing there answered. See ``docs/`` "Quality rating"."""
        check_score(score)
        if (call_id is None) == (trace_id is None):
            raise ValueError(
                "record_quality() takes exactly one of call_id= or trace_id= —"
                " a rating names the call it rates",
            )
        row = await self._resolve_rated(call_id=call_id, trace_id=trace_id)
        await self._store.record_quality(row.id, score, scope=self.scope)
        if self._learner is not None:
            self._learner.record_quality_observed(row.llm_name, row.operation, row.id, score)

    async def _resolve_rated(self, *, call_id: str | None, trace_id: str | None) -> Call:
        """The one call a rating key names: the newest that answered, inside the window.
        A trace naming more rows than one call's attempts is the host reusing it, which
        is reported — the rating still lands on exactly one call."""
        key = f"call_id={call_id!r}" if call_id is not None else f"trace_id={trace_id!r}"
        # A call id names one row; a trace names one call's attempts.
        limit = 1 if call_id is not None else _RATING_PAGE
        rows = await self.calls(
            limit=limit,
            since=datetime.now(UTC) - _RATING_WINDOW,
            call_id=call_id,
            trace_id=trace_id,
        )
        if trace_id is not None and len(rows) == _RATING_PAGE:
            logger.warning(
                "record_quality: %s matched the %d-row read bound — a trace names one"
                " call, so the rating lands on the newest that answered under it",
                key,
                _RATING_PAGE,
            )
        answered = next((row for row in rows if row.status is CallStatus.OK), None)
        if answered is None:
            raise UnknownCallError(
                f"no answered call found for {key} within the last"
                f" {_RATING_WINDOW.days} days — it may be older than that, purged by"
                " retention, or every attempt under it failed",
            )
        return answered

    # ------------------------------------------------------------------
    # Call journal — the rows this caller's scope is on
    # ------------------------------------------------------------------

    async def calls(
        self,
        *,
        limit: int,
        since: datetime | None = None,
        operation: str | None = None,
        trace_id: str | None = None,
        call_id: str | None = None,
    ) -> list[Call]:
        """Newest-first journal tail for this caller: one row per call attempt, each
        carrying the newest score it was rated with. The unscoped caller sees all.
        Never provisions the pool."""
        check_limit(limit)
        return await self._require_queryable().calls(
            limit=limit,
            scope=self.scope,
            since=to_utc(since, "since") if since is not None else None,
            operation=operation,
            trace_id=trace_id,
            call_id=call_id,
        )

    async def stats(
        self,
        *,
        since: datetime | None = None,
        limit: int = _DEFAULT_STATS_LIMIT,
        operation: str | None = None,
    ) -> Mapping[str, LLMStats]:
        """Per-model counts of this caller's call records over a window, keyed by model
        name. ``limit`` caps rows read, not the window: totals summing to it mean the
        window may be truncated. Never provisions the pool."""
        rows = await self.calls(limit=limit, since=since, operation=operation)
        return stats_from_calls(rows)

    def _require_queryable(self) -> QueryableStoreProtocol:
        if not isinstance(self._store, QueryableStoreProtocol):
            raise TypeError(
                "this store backend is not queryable — use a queryable backend"
                " (e.g. llmbroker.sqlite.Store) for calls()",
            )
        return self._store
