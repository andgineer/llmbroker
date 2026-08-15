"""``AsyncLLMs``: one caller's view of the installation's pool — the scope its
journal rows carry and the keys it may pay with. Built by the broker only."""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import aclosing
from datetime import datetime

import httpx

from llmbroker.broker.catalog import Catalog, find_declared
from llmbroker.broker.keyring import KeyRing
from llmbroker.broker.learning import Learner
from llmbroker.broker.pool_view import PoolView
from llmbroker.broker.result import AsyncLLM, AsyncResult
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

# A rating key resolves through a bounded read: one trace covers a request's
# attempts, and a host reusing one across a whole session gets the warning instead.
_RATING_PAGE = 100


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
        await self._ensure_pool()
        try:
            return await self._router.chat(
                self._ring,
                messages,
                tools=tools,
                operation=operation,
                trace_id=trace_id,
                wait=wait,
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
        )

    async def stream(
        self,
        prompt: str,
        *,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
    ) -> AsyncIterator[str]:
        """Route a completion over the pool and yield text deltas as they arrive.
        Fails over like ``ask`` until the first delta, then raises
        ``StreamInterruptedError``. Async-only."""
        await self._ensure_pool()
        messages = [{"role": "user", "content": prompt}]
        produced = False
        try:
            async with aclosing(
                self._router.stream(
                    self._ring,
                    messages,
                    operation=operation,
                    trace_id=trace_id,
                    wait=wait,
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
                operation=operation,
                trace_id=trace_id,
                wait=0,
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
        """Rate a past call, by exactly one key — a trace rates every attempt under it
        that answered. Raises ``UnknownCallError`` when the key names no answered call.
        See ``docs/`` "Quality rating"."""
        check_score(score)
        if (call_id is None) == (trace_id is None):
            raise ValueError(
                "record_quality() takes exactly one of call_id= or trace_id= —"
                " a rating names the call it rates",
            )
        rated = await self._resolve_rated(call_id=call_id, trace_id=trace_id)
        for row in rated:
            await self._store.record_quality(row.id, score, scope=self.scope)
            if self._learner is not None:
                self._learner.record_quality_observed(row.llm_name, row.operation, score)

    async def _resolve_rated(self, *, call_id: str | None, trace_id: str | None) -> list[Call]:
        """The answered attempts a rating key names. A page that comes back full may
        have left attempts unrated, so it is reported rather than silently truncated."""
        key = f"call_id={call_id!r}" if call_id is not None else f"trace_id={trace_id!r}"
        rows = await self.calls(limit=_RATING_PAGE, call_id=call_id, trace_id=trace_id)
        if len(rows) == _RATING_PAGE:
            logger.warning(
                "record_quality: %s resolved to %d rows, the read bound — attempts past"
                " it are not rated",
                key,
                _RATING_PAGE,
            )
        answered = [row for row in rows if row.status is CallStatus.OK]
        if not answered:
            raise UnknownCallError(
                f"no answered call found for {key} — retention may have purged it,"
                " or every attempt under it failed",
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
