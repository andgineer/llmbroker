"""``Learner``: the observer of the journal stream. Drives the optimizer's
bookkeeping from live events, and periodically re-derives everything else from one
cached read of the journal tail."""

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from llmbroker.broker.pool import BUDGET_BOUND_WINDOW_SEC, LLMPool
from llmbroker.broker.stats import stats_from_calls
from llmbroker.http_status import is_auth_failure
from llmbroker.models import Call, CallStatus, LLMMetrics, key_hash
from llmbroker.optimizer import Optimizer
from llmbroker.protocols.store import (
    DisabledMapProtocol,
    QueryableStoreProtocol,
    StoreProtocol,
)

logger = logging.getLogger("llmbroker.broker")

_REBUILD_TTL = 60.0  # seconds — checked on activity, no background task
TAIL_READ_LIMIT = 300


def metrics_from_calls(rows: list[Call]) -> dict[str, LLMMetrics]:
    """rows newest-first: the first call row per model is its most recent."""
    return {
        name: LLMMetrics(
            call_count=stats.total,
            last_status=stats.last_status,
            last_at=stats.last_at,
        )
        for name, stats in stats_from_calls(rows).items()
    }


def budget_bounds_from_calls(
    rows: list[Call],
    *,
    since: datetime,
) -> dict[str, tuple[float, datetime]]:
    """Per model, the largest budget it failed to answer within since ``since``, and
    when. Two things retire a miss and both are needed: the model's own success, and
    the clock — a model never picked never succeeds, so nothing else would clear it.
    """
    bounds: dict[str, tuple[float, datetime]] = {}
    answered: set[str] = set()
    for row in rows:  # newest-first
        if row.kind != "call" or row.llm_name in answered:
            continue
        if row.status == CallStatus.OK:
            answered.add(row.llm_name)
            continue
        if row.budget_ms is None or row.ts is None or row.ts < since:
            continue
        seconds = row.budget_ms / 1000
        current = bounds.get(row.llm_name)
        if current is None or seconds > current[0]:
            bounds[row.llm_name] = (seconds, row.ts)
    return bounds


class Learner:
    """Observes the journal stream: cooldown bookkeeping, dead-key drops, quality
    windows, budget bounds, debounced journal rebuild."""

    def __init__(
        self,
        optimizer: Optimizer,
        store: StoreProtocol,
        pool: LLMPool,
        resync_registry: Callable[[], Awaitable[None]],
        *,
        quality_rebuild_limit: int = TAIL_READ_LIMIT,
    ) -> None:
        self._opt = optimizer
        self._store = store
        self._pool = pool
        self._resync_registry = resync_registry
        self._quality_rebuild_limit = quality_rebuild_limit
        self._next_rebuild: float = 0.0
        self.metrics: dict[str, LLMMetrics] = {}

    def record_quality_observed(self, llm_name: str, operation: str | None, score: float) -> None:
        """Fold a rating the caller has already persisted into the live window."""
        self._opt.record_quality(llm_name, operation, score)

    async def observe(self, call: Call) -> None:
        name = call.llm_name
        if call.status in (CallStatus.RATE_LIMITED, CallStatus.UNAVAILABLE):
            self._opt.on_rate_limited(name)
            await self.maybe_rebuild(force=True)
        elif call.status == CallStatus.OK:
            self._opt.on_success(name)
        elif call.status == CallStatus.ERROR:
            # A failure that cooled or dropped nothing has nothing to propagate, and a
            # spent wait budget would otherwise rebuild on every such call.
            shared = True
            if call.http_status is not None and is_auth_failure(call.http_status):
                cfg = self._pool.configs.get(name)
                ref = cfg.api_key_ref if cfg else "unknown"
                await self._pool.drop(name)
                logger.error(
                    "%s: API key appears dead (HTTP %s) — check api_key_ref %r",
                    name,
                    call.http_status,
                    ref,
                )
            elif call.cooldown_until is not None:
                # Only a failure that actually cooled the model feeds its streak:
                # a client-side 4xx and a spent wait budget are not its fault.
                self._opt.on_rate_limited(name)
            else:
                shared = False
            await self.maybe_rebuild(force=shared)

    async def maybe_rebuild(self, *, force: bool = False, resync_registry: bool = True) -> None:
        """Re-derive everything from one cached tail read, and re-read the registry and
        disabled map so other nodes' edits propagate. Debounced unless ``force``;
        ``resync_registry=False`` is for the warm start, which just resynced."""
        now = time.monotonic()
        if not force and now < self._next_rebuild:
            return
        self._next_rebuild = now + _REBUILD_TTL
        if resync_registry:
            await self._safe_resync_registry()
        if isinstance(self._store, QueryableStoreProtocol):
            rows = await self._store.calls(limit=self._quality_rebuild_limit)
            self._apply_scores_and_metrics(rows)
            await self._apply_peer_effects(rows)
        await self._resync_disabled()

    async def _safe_resync_registry(self) -> None:
        """Picking up another process's edits must never fail the call that carried it
        here — this runs off the journal write, so anything raised would surface out of
        the user's own ``ask``."""
        try:
            await self._resync_registry()
        except Exception:  # noqa: BLE001 - a background re-read may not break a request
            logger.exception("registry resync failed, continuing on the current pool")

    def _own_key_hash(self, name: str) -> str | None:
        if not self._pool.has_key(name):
            return None
        return key_hash(self._pool.resolved_key(name))

    def _apply_scores_and_metrics(self, rows: list[Call]) -> None:
        """rows are newest-first: keep the newest ``quality_window`` ratings per
        bucket."""
        scores: dict[tuple[str, str | None], list[float]] = {}
        for row in rows:
            if row.kind != "quality":
                continue
            key = (row.llm_name, row.operation)
            bucket = scores.setdefault(key, [])
            if len(bucket) < self._opt.quality_window:
                bucket.append(row.quality_score if row.quality_score is not None else 0.0)
        self._opt.load_scores(scores)
        self.metrics = metrics_from_calls(rows)

    def _cooldown_applies(self, row: Call) -> bool:
        """5xx (``UNAVAILABLE``/other ``ERROR``) applies unconditionally — provider-side,
        shared by everyone; 429/401/403 quota failures only where the key hash matches."""
        is_quota_failure = row.status == CallStatus.RATE_LIMITED or (
            row.status == CallStatus.ERROR
            and row.http_status is not None
            and is_auth_failure(row.http_status)
        )
        if not is_quota_failure:
            return True
        return row.key_hash is not None and row.key_hash == self._own_key_hash(row.llm_name)

    async def _apply_peer_effects(self, rows: list[Call]) -> None:
        cooldowns: dict[str, datetime] = {}
        fail_counts: dict[str, int] = {}
        dead: set[str] = set()
        for row in rows:
            if row.kind != "call" or row.cooldown_until is None:
                continue
            fail_counts[row.llm_name] = fail_counts.get(row.llm_name, 0) + 1
            if self._cooldown_applies(row):
                current = cooldowns.get(row.llm_name)
                if current is None or row.cooldown_until > current:
                    cooldowns[row.llm_name] = row.cooldown_until
            if (
                row.http_status is not None
                and is_auth_failure(row.http_status)
                and row.key_hash is not None
                and row.key_hash == self._own_key_hash(row.llm_name)
            ):
                dead.add(row.llm_name)
        await self._pool.apply_peer_cooldowns(cooldowns, fail_counts)
        window_start = datetime.now(UTC) - timedelta(seconds=BUDGET_BOUND_WINDOW_SEC)
        await self._pool.apply_budget_bounds(budget_bounds_from_calls(rows, since=window_start))
        for name in dead:
            await self._pool.drop(name)

    async def _resync_disabled(self) -> None:
        if not isinstance(self._store, DisabledMapProtocol):
            return
        await self._store.seed_disabled(list(self._pool.configs))
        disabled = await self._store.disabled_map()
        for name, flag in disabled.items():
            if flag:
                self._pool.set_disabled(name)
            else:
                await self._pool.clear_disabled(name)
