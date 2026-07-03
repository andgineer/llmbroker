"""Redis-backed state store over Redis hashes.

One hash per ``(user_id)`` scope.  Key format:
- ``llmbroker_state:__none__`` when ``user_id`` is ``None``
- ``llmbroker_state:{user_id}`` for any other value

Learned summaries share one hash per scope too (``llmbroker_summaries:{scope}``),
with each ``(name, operation, kind)`` triple stored as four numeric hash fields
(``...:w``/``...:g``/``...:s``/``...:c``). The fused multiply-add fold is done as
an optimistic-locking transaction (``WATCH``/``MULTI``/``EXEC``, retried on
``WatchError``) rather than a server-side Lua script — equally atomic (no lost
updates under concurrency) and portable to any redis-py-compatible client.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Self

import redis.asyncio as aioredis
from redis.exceptions import WatchError

from llmbroker.models import LifecyclePhase, LLMState, QualitySummary, check_user_id, reconcile

_NONE_OPERATION = "\x00__none__\x00"
_FIELD_SEP = "\x1f"
_MAX_CAS_RETRIES = 50


def _op_token(operation: str | None) -> str:
    return _NONE_OPERATION if operation is None else operation


def _op_from_token(token: str) -> str | None:
    return None if token == _NONE_OPERATION else token


class StateStore:
    """Redis-backed state store implementing ``StateStoreProtocol``.

    Accepts a pre-built async Redis client.  Use ``from_url`` to construct
    from a connection URL.
    """

    def __init__(self, client: aioredis.Redis) -> None:
        self._client = client

    @classmethod
    def from_url(cls, url: str, **kwargs: object) -> Self:
        return cls(aioredis.from_url(url, decode_responses=True, **kwargs))

    def _scope_key(self, user_id: int | str | None) -> str:
        if user_id is None:
            return "llmbroker_state:__none__"
        return f"llmbroker_state:{user_id}"

    def _summaries_key(self, user_id: int | str | None) -> str:
        if user_id is None:
            return "llmbroker_summaries:__none__"
        return f"llmbroker_summaries:{user_id}"

    def _summary_fields(self, name: str, operation: str | None, kind: str) -> tuple[str, ...]:
        """Build the four hash-field names for one ``(name, operation, kind)`` summary.

        ``_FIELD_SEP`` must not appear in any part — it delimits the encoded field
        name, and ``read_summaries`` decodes it by splitting on that same separator.
        """
        op_token = _op_token(operation)
        for part in (name, op_token, kind):
            if _FIELD_SEP in part:
                raise ValueError(
                    f"redis state store: {part!r} must not contain {_FIELD_SEP!r}",
                )
        prefix = _FIELD_SEP.join((name, op_token, kind))
        return tuple(f"{prefix}{_FIELD_SEP}{suffix}" for suffix in ("w", "g", "s", "c"))

    async def read(self, user_id: int | str | None = None) -> dict[str, LLMState]:
        check_user_id(user_id)
        raw = await self._client.hgetall(self._scope_key(user_id))
        now = datetime.now(UTC)
        return {
            str(name): reconcile(LLMState.from_dict(json.loads(value)), now)
            for name, value in raw.items()
        }

    async def write(self, name: str, state: LLMState, user_id: int | str | None = None) -> None:
        check_user_id(user_id)
        if state.cooldown_until is not None and state.cooldown_until.tzinfo is None:
            raise ValueError("LLMState.cooldown_until must be timezone-aware")
        cooldown_until = (
            state.cooldown_until
            if state.cooldown_until is not None and state.phase is not LifecyclePhase.AVAILABLE
            else None
        )
        payload = replace(state, cooldown_until=cooldown_until).to_dict()
        await self._client.hset(self._scope_key(user_id), name, json.dumps(payload))

    async def apply_summary_delta(  # noqa: PLR0913
        self,
        name: str,
        operation: str | None,
        kind: str,
        decay_pow: float,
        add_weight: float,
        add_good: float,
        add_weight_sq: float,
        add_count: int,
        user_id: int | str | None = None,
    ) -> None:
        check_user_id(user_id)
        key = self._summaries_key(user_id)
        wf, gf, sf, cf = self._summary_fields(name, operation, kind)
        decay_pow_sq = decay_pow * decay_pow
        for _ in range(_MAX_CAS_RETRIES):
            async with self._client.pipeline(transaction=True) as pipe:
                await pipe.watch(key)
                raw = await pipe.hmget(key, wf, gf, sf, cf)
                w = float(raw[0]) if raw[0] is not None else 0.0
                g = float(raw[1]) if raw[1] is not None else 0.0
                s = float(raw[2]) if raw[2] is not None else 0.0
                c = float(raw[3]) if raw[3] is not None else 0.0
                pipe.multi()
                pipe.hset(
                    key,
                    mapping={
                        wf: w * decay_pow + add_weight,
                        gf: g * decay_pow + add_good,
                        sf: s * decay_pow_sq + add_weight_sq,
                        cf: c + add_count,
                    },
                )
                try:
                    await pipe.execute()
                    return
                except WatchError:
                    continue
        raise RuntimeError(
            f"apply_summary_delta: exceeded {_MAX_CAS_RETRIES} retries under contention"
            f" for {name!r}/{operation!r}/{kind!r}",
        )

    async def read_summaries(
        self,
        user_id: int | str | None = None,
    ) -> dict[tuple[str, str | None, str], QualitySummary]:
        check_user_id(user_id)
        raw = await self._client.hgetall(self._summaries_key(user_id))
        grouped: dict[tuple[str, str | None, str], dict[str, float]] = {}
        for field, value in raw.items():
            name, op_token, kind, suffix = str(field).split(_FIELD_SEP)
            grouped.setdefault((name, _op_from_token(op_token), kind), {})[suffix] = float(value)
        return {
            key: QualitySummary(
                weight=parts.get("w", 0.0),
                weighted_good=parts.get("g", 0.0),
                weight_sq=parts.get("s", 0.0),
                count=int(parts.get("c", 0.0)),
            )
            for key, parts in grouped.items()
        }

    async def seed_summary(
        self,
        name: str,
        operation: str | None,
        kind: str,
        summary: QualitySummary,
        user_id: int | str | None = None,
    ) -> None:
        """Insert-if-absent, idempotent across racing instances (WATCH/MULTI/EXEC CAS)."""
        check_user_id(user_id)
        key = self._summaries_key(user_id)
        wf, gf, sf, cf = self._summary_fields(name, operation, kind)
        for _ in range(_MAX_CAS_RETRIES):
            async with self._client.pipeline(transaction=True) as pipe:
                await pipe.watch(key)
                if await pipe.hexists(key, wf):
                    return
                pipe.multi()
                pipe.hset(
                    key,
                    mapping={
                        wf: summary.weight,
                        gf: summary.weighted_good,
                        sf: summary.weight_sq,
                        cf: summary.count,
                    },
                )
                try:
                    await pipe.execute()
                    return
                except WatchError:
                    continue
        raise RuntimeError(
            f"seed_summary: exceeded {_MAX_CAS_RETRIES} retries under contention"
            f" for {name!r}/{operation!r}/{kind!r}",
        )

    async def aclose(self) -> None:
        return
