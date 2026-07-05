"""MongoDB-backed queryable telemetry over ``llmbroker_calls`` + the
``llmbroker_disabled`` admin verdict map."""

import uuid
from datetime import UTC, datetime

from motor.motor_asyncio import AsyncIOMotorDatabase

from llmbroker.models import Call, CallStatus, LLMMetrics, Usage
from llmbroker.mongodb.schema import ensure_schema, ensure_utc


def _call_from_doc(doc: dict) -> Call:
    usage_extra = doc.get("usage_extra")
    usage = None
    pt = doc.get("prompt_tokens")
    ct = doc.get("completion_tokens")
    tt = doc.get("total_tokens")
    if any(v is not None for v in (pt, ct, tt, usage_extra)):
        usage = Usage(
            prompt_tokens=pt,
            completion_tokens=ct,
            total_tokens=tt,
            extra=usage_extra,
        )
    status = doc.get("status")
    return Call(
        id=str(doc["_id"]),
        llm_name=str(doc["llm_name"]),
        operation=doc.get("operation"),
        trace_id=doc.get("trace_id"),
        status=CallStatus(status) if status is not None else None,
        kind=doc.get("kind", "call"),
        ts=ensure_utc(doc.get("called_at")),
        http_status=doc.get("http_status"),
        latency_ms=doc.get("latency_ms"),
        error_detail=doc.get("error_detail"),
        usage=usage,
        quality_score=doc.get("quality_score"),
        call_id=doc.get("call_id"),
        scope=doc.get("scope"),
        cooldown_until=ensure_utc(doc.get("cooldown_until")),
        key_hash=doc.get("key_hash"),
    )


class Telemetry:
    """MongoDB-backed queryable telemetry over ``llmbroker_calls`` + admin verdicts."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db

    async def record(self, call: Call) -> None:
        await ensure_schema(self._db)
        usage_extra = None
        pt = ct = tt = None
        if call.usage is not None:
            pt = call.usage.prompt_tokens
            ct = call.usage.completion_tokens
            tt = call.usage.total_tokens
            usage_extra = call.usage.extra
        doc = {
            "_id": call.id,
            "llm_name": call.llm_name,
            "operation": call.operation,
            "trace_id": call.trace_id,
            "status": call.status.value if call.status is not None else None,
            "kind": call.kind,
            "http_status": call.http_status,
            "latency_ms": call.latency_ms,
            "error_detail": call.error_detail,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": tt,
            "usage_extra": usage_extra,
            "quality_score": call.quality_score,
            "call_id": call.call_id,
            "called_at": call.ts or datetime.now(UTC),
            "scope": call.scope,
            "cooldown_until": call.cooldown_until,
            "key_hash": call.key_hash,
        }
        await self._db["llmbroker_calls"].insert_one(doc)

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

    async def metrics(
        self,
        *,
        since: datetime | None = None,
        user_id: int | str | None = None,
    ) -> dict[str, LLMMetrics]:
        """Unused by the broker (metrics are served from the rebuild's cached tail);
        kept for hosts that want a direct read."""
        await ensure_schema(self._db)
        match: dict = {"scope": user_id, "kind": "call"}
        if since is not None:
            match["called_at"] = {"$gte": since}
        pipeline = [
            {"$match": match},
            {"$sort": {"called_at": 1}},
            {
                "$group": {
                    "_id": "$llm_name",
                    "call_count": {"$sum": 1},
                    "last_at": {"$max": "$called_at"},
                    "last_status": {"$last": "$status"},
                },
            },
        ]
        docs = await self._db["llmbroker_calls"].aggregate(pipeline).to_list(length=None)
        return {
            doc["_id"]: LLMMetrics(
                call_count=doc["call_count"],
                last_status=CallStatus(doc["last_status"]) if doc["last_status"] else None,
                last_at=ensure_utc(doc["last_at"]),
            )
            for doc in docs
        }

    async def calls(self, *, limit: int, scope: str | None = None) -> list[Call]:
        """Newest-first tail of the journal, both kinds interleaved — unfiltered by scope
        (learning is global); ``scope`` is accepted for the host-facing filter only."""
        await ensure_schema(self._db)
        query: dict = {} if scope is None else {"scope": scope}
        cursor = self._db["llmbroker_calls"].find(query).sort("called_at", -1).limit(limit)
        docs = await cursor.to_list(length=None)
        return [_call_from_doc(d) for d in docs]

    async def purge_calls(self, *, before: datetime) -> int:
        """Delete all calls older than *before*, across all scopes. Admin operation."""
        await ensure_schema(self._db)
        result = await self._db["llmbroker_calls"].delete_many(
            {"called_at": {"$lt": before}},
        )
        return result.deleted_count

    # ------------------------------------------------------------------
    # Admin disabled-verdict map
    # ------------------------------------------------------------------

    async def get_disabled(self, name: str) -> bool:
        await ensure_schema(self._db)
        doc = await self._db["llmbroker_disabled"].find_one({"name": name})
        return bool(doc["disabled"]) if doc else False

    async def set_disabled(self, name: str, flag: bool) -> None:  # noqa: FBT001
        await ensure_schema(self._db)
        await self._db["llmbroker_disabled"].update_one(
            {"name": name},
            {"$set": {"disabled": flag}},
            upsert=True,
        )

    async def seed_disabled(self, names: list[str]) -> None:
        """Insert-if-absent every name with ``disabled=False`` — never touches existing values."""
        await ensure_schema(self._db)
        for name in names:
            await self._db["llmbroker_disabled"].update_one(
                {"name": name},
                {"$setOnInsert": {"disabled": False}},
                upsert=True,
            )

    async def disabled_map(self) -> dict[str, bool]:
        await ensure_schema(self._db)
        docs = await self._db["llmbroker_disabled"].find({}).to_list(length=None)
        return {str(d["name"]): bool(d["disabled"]) for d in docs}

    async def aclose(self) -> None:
        return
