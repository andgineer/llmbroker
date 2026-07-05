"""``_LearningHook``: the wrapper around a knowledge backend.

Drives ``Optimizer`` bookkeeping (backoff counters, quality windows) from the
live event stream, and periodically rebuilds derived state — quality-window
verdicts, shared cooldowns, snapshot metrics, registry membership, and the
admin disabled-verdict map — from one cached read of the journal tail. No
second storage subsystem: everything llmbroker learns beyond config is
re-derived from the append-only journal plus the tiny disabled map.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

from llmbroker.broker.pool import LLMPool
from llmbroker.models import Call, CallStatus, LLMMetrics, key_hash
from llmbroker.optimizer import Optimizer
from llmbroker.protocols.knowledge import (
    DisabledMapProtocol,
    KnowledgeProtocol,
    QueryableKnowledgeProtocol,
)

logger = logging.getLogger("llmbroker.broker")

_REBUILD_TTL = 60.0  # seconds — checked on activity, no background task
_DEFAULT_QUALITY_REBUILD_LIMIT = 300
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403


class _LearningHook:
    """Knowledge hook: cooldown bookkeeping, dead-key drops, quality windows,
    debounced journal rebuild."""

    def __init__(
        self,
        optimizer: Optimizer,
        inner: KnowledgeProtocol,
        pool: LLMPool,
        resync_registry: Callable[[], Awaitable[None]],
        *,
        quality_rebuild_limit: int = _DEFAULT_QUALITY_REBUILD_LIMIT,
    ) -> None:
        self._opt = optimizer
        self._inner = inner
        self._pool = pool
        self._resync_registry = resync_registry
        self._quality_rebuild_limit = quality_rebuild_limit
        self._next_rebuild: float = 0.0
        self.metrics_cache: dict[str, LLMMetrics] = {}

    async def record(self, call: Call) -> None:
        try:
            await self._inner.record(call)
        finally:
            await self._drive(call)

    async def record_quality(
        self,
        llm_name: str,
        operation: str | None,
        score: float,
        *,
        call_id: str | None = None,
    ) -> None:
        await self._inner.record_quality(llm_name, operation, score, call_id=call_id)
        self._opt.record_quality(llm_name, operation, score)

    async def calls(self, *, limit: int, scope: str | None = None) -> list[Call]:
        if isinstance(self._inner, QueryableKnowledgeProtocol):
            return await self._inner.calls(limit=limit, scope=scope)
        return []

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def _drive(self, call: Call) -> None:
        name = call.llm_name
        if call.status in (CallStatus.RATE_LIMITED, CallStatus.UNAVAILABLE):
            self._opt.on_rate_limited(name)
            await self.maybe_rebuild(force=True)
        elif call.status == CallStatus.OK:
            self._opt.on_success(name)
        elif call.status == CallStatus.ERROR:
            if call.http_status in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN):
                cfg = self._pool.configs.get(name)
                ref = cfg.api_key_ref if cfg else "unknown"
                await self._pool.drop(name)
                logger.error(
                    "%s: API key appears dead (HTTP %s) — check api_key_ref %r",
                    name,
                    call.http_status,
                    ref,
                )
            else:
                self._opt.on_rate_limited(name)
            await self.maybe_rebuild(force=True)

    async def maybe_rebuild(self, *, force: bool = False, resync_registry: bool = True) -> None:
        """Re-derive score windows, shared cooldowns, and metrics from one cached tail
        read; re-read the registry and disabled map so edits from other processes/nodes
        propagate. Debounced to at most once per ``_REBUILD_TTL`` unless ``force``.

        ``resync_registry=False`` skips the registry re-read — used for the
        provision-time warm start, where ``Catalog.provision()`` just resynced it.
        """
        now = time.monotonic()
        if not force and now < self._next_rebuild:
            return
        self._next_rebuild = now + _REBUILD_TTL
        if isinstance(self._inner, QueryableKnowledgeProtocol):
            rows = await self._inner.calls(limit=self._quality_rebuild_limit)
            self._apply_scores_and_metrics(rows)
            await self._apply_peer_effects(rows)
        if resync_registry:
            await self._resync_registry()
        await self._resync_disabled()

    def _own_key_hash(self, name: str) -> str | None:
        if not self._pool.has_key(name):
            return None
        return key_hash(self._pool.resolved_key(name))

    def _apply_scores_and_metrics(self, rows: list[Call]) -> None:
        """rows are newest-first: keep the newest ``quality_window`` ratings per
        bucket, and the first (= most recent) call row per model for metrics."""
        scores: dict[tuple[str, str | None], list[float]] = {}
        metrics: dict[str, LLMMetrics] = {}
        for row in rows:
            if row.kind == "quality":
                key = (row.llm_name, row.operation)
                bucket = scores.setdefault(key, [])
                if len(bucket) < self._opt.quality_window:
                    bucket.append(row.quality_score if row.quality_score is not None else 0.0)
                continue
            existing = metrics.get(row.llm_name)
            if existing is None:
                metrics[row.llm_name] = LLMMetrics(
                    call_count=1,
                    last_status=row.status,
                    last_at=row.ts,
                )
            else:
                metrics[row.llm_name] = LLMMetrics(
                    call_count=existing.call_count + 1,
                    last_status=existing.last_status,
                    last_at=existing.last_at,
                )
        self._opt.load_scores(scores)
        self.metrics_cache = metrics

    def _cooldown_applies(self, row: Call) -> bool:
        """5xx (``UNAVAILABLE``/other ``ERROR``) applies unconditionally — provider-side,
        shared by everyone; 429/401/403 quota failures only where the key hash matches."""
        is_quota_failure = row.status == CallStatus.RATE_LIMITED or (
            row.status == CallStatus.ERROR
            and row.http_status in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN)
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
                row.http_status in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN)
                and row.key_hash is not None
                and row.key_hash == self._own_key_hash(row.llm_name)
            ):
                dead.add(row.llm_name)
        await self._pool.apply_peer_cooldowns(cooldowns, fail_counts)
        for name in dead:
            await self._pool.drop(name)

    async def _resync_disabled(self) -> None:
        if not isinstance(self._inner, DisabledMapProtocol):
            return
        await self._inner.seed_disabled(list(self._pool.configs))
        disabled = await self._inner.disabled_map()
        for name, flag in disabled.items():
            if flag:
                self._pool.set_disabled(name)
            else:
                await self._pool.clear_disabled(name)
