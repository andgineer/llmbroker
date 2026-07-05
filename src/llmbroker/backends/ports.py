"""Generic ports: the domain protocols implemented once over any ``Driver``.

Registry and secrets are global — no user scope exists anywhere in this layer
(the broker turns an opaque ``scope`` string into a secret-ref prefix and a
journal attribution field instead).
"""

import time
import uuid
from datetime import UTC, datetime, timedelta

from llmbroker.backends.driver import Driver, Row
from llmbroker.models import Call, CallStatus, LLMConfig, Usage

_DEFAULT_RETENTION = timedelta(days=90)
_PURGE_INTERVAL_SECONDS = 3600.0


class DriverRegistry:
    """Registry over any ``Driver`` — a pure preset mirror, globally scoped."""

    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    async def load(self) -> list[LLMConfig]:
        rows = await self._driver.fetch("registry")
        return [
            LLMConfig.from_metadata(
                name=str(row["name"]),
                base_url=str(row["base_url"]),
                model=str(row["model"]),
                api_key_ref=str(row["api_key_ref"]),
                metadata=row.get("metadata"),  # type: ignore[arg-type]
            )
            for row in rows
        ]

    async def mirror(self, configs: list[LLMConfig]) -> None:
        """Total mirror: add new, update existing, delete stored entries absent
        from ``configs`` — the only registry write path."""
        source_names = {c.name for c in configs}
        existing = {str(row["name"]) for row in await self._driver.fetch("registry")}
        for name in existing - source_names:
            await self._driver.delete("registry", (name,))
        for cfg in configs:
            await self._driver.upsert(
                "registry",
                (cfg.name,),
                {
                    "name": cfg.name,
                    "base_url": cfg.base_url,
                    "model": cfg.model,
                    "api_key_ref": cfg.api_key_ref,
                    "metadata": cfg.to_metadata(),
                },
            )

    async def aclose(self) -> None:
        await self._driver.aclose()


def _usage_columns(usage: Usage | None) -> dict[str, object]:
    if usage is None:
        return {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "usage_extra": None,
        }
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "usage_extra": usage.extra,
    }


def _call_to_row(call: Call) -> Row:
    row: Row = {
        "id": call.id,
        "llm_name": call.llm_name,
        "operation": call.operation,
        "trace_id": call.trace_id,
        "status": call.status.value if call.status is not None else None,
        "kind": call.kind,
        "http_status": call.http_status,
        "latency_ms": call.latency_ms,
        "error_detail": call.error_detail,
        "quality_score": call.quality_score,
        "call_id": call.call_id,
        "called_at": call.ts or datetime.now(UTC),
        "scope": call.scope,
        "cooldown_until": call.cooldown_until,
        "key_hash": call.key_hash,
    }
    row.update(_usage_columns(call.usage))
    return row


def _row_to_call(row: Row) -> Call:
    pt, ct, tt = row.get("prompt_tokens"), row.get("completion_tokens"), row.get("total_tokens")
    extra = row.get("usage_extra")
    usage = None
    if any(v is not None for v in (pt, ct, tt, extra)):
        usage = Usage(
            prompt_tokens=pt,  # type: ignore[arg-type]
            completion_tokens=ct,  # type: ignore[arg-type]
            total_tokens=tt,  # type: ignore[arg-type]
            extra=extra,  # type: ignore[arg-type]
        )
    status = row.get("status")
    return Call(
        id=str(row["id"]),
        llm_name=str(row["llm_name"]),
        operation=row.get("operation"),  # type: ignore[arg-type]
        trace_id=row.get("trace_id"),  # type: ignore[arg-type]
        status=CallStatus(status) if status is not None else None,
        kind=str(row.get("kind", "call")),
        ts=row.get("called_at"),  # type: ignore[arg-type]
        http_status=row.get("http_status"),  # type: ignore[arg-type]
        latency_ms=row.get("latency_ms"),  # type: ignore[arg-type]
        error_detail=row.get("error_detail"),  # type: ignore[arg-type]
        usage=usage,
        quality_score=row.get("quality_score"),  # type: ignore[arg-type]
        call_id=row.get("call_id"),  # type: ignore[arg-type]
        scope=row.get("scope"),  # type: ignore[arg-type]
        cooldown_until=row.get("cooldown_until"),  # type: ignore[arg-type]
        key_hash=row.get("key_hash"),  # type: ignore[arg-type]
    )


class DriverStore:
    """Journal (append/recent/purge) + admin disabled-map, over any ``Driver``.

    Self-purges call rows older than ``retention``, checked at most once per
    hour on write activity.
    """

    def __init__(self, driver: Driver, *, retention: timedelta = _DEFAULT_RETENTION) -> None:
        self._driver = driver
        self._retention = retention
        self._last_purge = float("-inf")

    async def record(self, call: Call) -> None:
        await self._driver.append("calls", _call_to_row(call))
        await self._maybe_purge()

    async def record_quality(
        self,
        llm_name: str,
        operation: str | None,
        score: float,
        *,
        call_id: str | None = None,
    ) -> None:
        """Append a self-contained quality record — never updates the call row."""
        await self.record(
            Call(
                id=str(uuid.uuid4()),
                llm_name=llm_name,
                operation=operation,
                trace_id=None,
                status=None,
                kind="quality",
                ts=datetime.now(UTC),
                quality_score=score,
                call_id=call_id,
            ),
        )

    async def calls(self, *, limit: int, scope: str | None = None) -> list[Call]:
        """Newest-first tail of the journal, both kinds interleaved — unfiltered by scope
        (learning is global); ``scope`` is accepted for the host-facing filter only."""
        match = {"scope": scope} if scope is not None else None
        rows = await self._driver.recent("calls", limit, match)
        return [_row_to_call(r) for r in rows]

    async def _purge_old_calls(self) -> None:
        cutoff = datetime.now(UTC) - self._retention
        await self._driver.purge("calls", cutoff)

    async def _maybe_purge(self) -> None:
        now = time.monotonic()
        if now - self._last_purge < _PURGE_INTERVAL_SECONDS:
            return
        self._last_purge = now
        await self._purge_old_calls()

    # ------------------------------------------------------------------
    # Admin disabled-verdict map
    # ------------------------------------------------------------------

    async def get_disabled(self, name: str) -> bool:
        row = await self._driver.get("disabled", (name,))
        return bool(row["disabled"]) if row else False

    async def set_disabled(self, name: str, flag: bool) -> None:  # noqa: FBT001
        await self._driver.upsert("disabled", (name,), {"name": name, "disabled": int(flag)})

    async def seed_disabled(self, names: list[str]) -> None:
        """Insert-if-absent every name with ``disabled=False`` — never touches existing values."""
        existing = {str(row["name"]) for row in await self._driver.fetch("disabled")}
        for name in names:
            if name not in existing:
                await self._driver.upsert("disabled", (name,), {"name": name, "disabled": 0})

    async def disabled_map(self) -> dict[str, bool]:
        rows = await self._driver.fetch("disabled")
        return {str(r["name"]): bool(r["disabled"]) for r in rows}

    async def aclose(self) -> None:
        await self._driver.aclose()


class DriverSecrets:
    """Flat ``ref -> value`` secrets store over any ``Driver``. Exact-match lookups
    only — the own→shared prefix fallback lives in the broker."""

    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    async def resolve(self, ref: str) -> str:
        row = await self._driver.get("secrets", (ref,))
        if row is None:
            raise KeyError(f"{type(self).__name__}: ref {ref!r} not found")
        return str(row["value"])

    async def set(self, ref: str, value: str) -> None:
        await self._driver.upsert("secrets", (ref,), {"ref": ref, "value": value})

    async def aclose(self) -> None:
        await self._driver.aclose()
