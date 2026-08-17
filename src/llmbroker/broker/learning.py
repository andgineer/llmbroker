"""``Learner``: the observer of the journal stream. Drives the optimizer's
bookkeeping from live events, and re-derives what the pool learned from one read
of the journal tail when it is rebuilt."""

import logging
from datetime import UTC, datetime, timedelta

from llmbroker.broker.pool import BUDGET_BOUND_WINDOW_SEC, LATENCY_SAMPLES, LLMPool
from llmbroker.broker.stats import stats_from_calls
from llmbroker.http_status import is_auth_failure
from llmbroker.models import Call, CallStatus, LLMMetrics
from llmbroker.optimizer import Optimizer
from llmbroker.protocols.store import QueryableStoreProtocol, StoreProtocol

logger = logging.getLogger("llmbroker.broker")

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
        if row.llm_name in answered:
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


def observed_latencies_from_calls(rows: list[Call]) -> dict[str, list[float]]:
    """Per model, what its most recent answers took, oldest-first. A stream contributes
    the wait for its first delta — which no consumer can move — and a completion its
    whole time; a row carrying neither is not evidence."""
    samples: dict[str, list[float]] = {}
    for row in rows:  # newest-first
        if row.status != CallStatus.OK:
            continue
        elapsed_ms = row.first_delta_ms if row.first_delta_ms is not None else row.latency_ms
        if elapsed_ms is None:
            continue
        bucket = samples.setdefault(row.llm_name, [])
        if len(bucket) < LATENCY_SAMPLES:
            bucket.append(elapsed_ms / 1000)
    return {name: bucket[::-1] for name, bucket in samples.items()}


class Learner:
    """Observes the journal stream: this process's own cooldown bookkeeping and dead-key
    drops, and the re-derivation a rebuild asks for."""

    def __init__(
        self,
        optimizer: Optimizer,
        store: StoreProtocol,
        pool: LLMPool,
        *,
        quality_rebuild_limit: int = TAIL_READ_LIMIT,
    ) -> None:
        self._opt = optimizer
        self._store = store
        self._pool = pool
        self._quality_rebuild_limit = quality_rebuild_limit
        self.metrics: dict[str, LLMMetrics] = {}

    def record_quality_observed(
        self,
        llm_name: str,
        operation: str | None,
        call_id: str,
        score: float,
    ) -> None:
        """Fold a rating the caller has already persisted into the live window."""
        self._opt.record_quality(llm_name, operation, call_id, score)

    async def observe(self, call: Call) -> None:
        """Apply what this process just learned, in this process. Nothing here is
        written for a peer and nothing reads a peer's (invariant 11)."""
        name = call.llm_name
        if call.status in (CallStatus.RATE_LIMITED, CallStatus.UNAVAILABLE):
            self._opt.on_rate_limited(name)
        elif call.status == CallStatus.OK:
            self._opt.on_success(name)
        elif call.status == CallStatus.ERROR:
            if call.http_status is not None and is_auth_failure(call.http_status):
                # The withdrawal itself belongs to the ring that paid: a key one
                # caller had revoked says nothing about another caller's.
                cfg = self._pool.configs.get(name)
                ref = cfg.api_key_ref if cfg else "unknown"
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

    async def relearn(self) -> None:
        """Re-derive quality, the two latency numbers and the snapshot metrics from one
        read of the journal tail. The rebuild's last step — and the only read of it
        (invariant 8)."""
        if not isinstance(self._store, QueryableStoreProtocol):
            return
        rows = await self._store.calls(limit=self._quality_rebuild_limit)
        self._apply_scores_and_metrics(rows)
        window_start = datetime.now(UTC) - timedelta(seconds=BUDGET_BOUND_WINDOW_SEC)
        await self._pool.apply_budget_bounds(budget_bounds_from_calls(rows, since=window_start))
        await self._pool.apply_latencies(observed_latencies_from_calls(rows))

    def _apply_scores_and_metrics(self, rows: list[Call]) -> None:
        """rows are newest-first: keep the newest ``quality_window`` rated calls per
        bucket, handed over oldest-first as the window expects."""
        scores: dict[tuple[str, str | None], list[tuple[str, float]]] = {}
        for row in rows:
            if row.score is None:
                continue
            bucket = scores.setdefault((row.llm_name, row.operation), [])
            if len(bucket) < self._opt.quality_window:
                bucket.append((row.id, row.score))
        self._opt.load_scores({key: bucket[::-1] for key, bucket in scores.items()})
        self.metrics = metrics_from_calls(rows)
