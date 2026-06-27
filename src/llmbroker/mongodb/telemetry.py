"""MongoDB-backed queryable telemetry over ``llmbroker_calls``."""

from datetime import UTC, datetime

from motor.motor_asyncio import AsyncIOMotorDatabase

from llmbroker.models import Call, CallStatus, LLMMetrics, Usage, check_user_id
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
    return Call(
        id=str(doc["_id"]),
        llm_name=str(doc["llm_name"]),
        operation=doc.get("operation"),
        trace_id=doc.get("trace_id"),
        status=CallStatus(doc["status"]),
        http_status=doc.get("http_status"),
        latency_ms=doc.get("latency_ms"),
        error_detail=doc.get("error_detail"),
        usage=usage,
        quality_score=doc.get("quality_score"),
        user_id=doc.get("user_id"),
    )


class Telemetry:
    """MongoDB-backed queryable telemetry over ``llmbroker_calls``."""

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
            "status": call.status.value,
            "http_status": call.http_status,
            "latency_ms": call.latency_ms,
            "error_detail": call.error_detail,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": tt,
            "usage_extra": usage_extra,
            "quality_score": call.quality_score,
            "called_at": datetime.now(UTC),
            "user_id": call.user_id,
        }
        await self._db["llmbroker_calls"].insert_one(doc)

    async def record_quality(self, call_id: str, score: float) -> None:
        await ensure_schema(self._db)
        result = await self._db["llmbroker_calls"].update_one(
            {"_id": call_id},
            {"$set": {"quality_score": score}},
        )
        if result.matched_count == 0:
            raise KeyError(call_id)

    async def metrics(
        self,
        *,
        since: datetime | None = None,
        user_id: int | str | None = None,
    ) -> dict[str, LLMMetrics]:
        check_user_id(user_id)
        await ensure_schema(self._db)
        match: dict = {"user_id": user_id}
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
                last_status=CallStatus(doc["last_status"]),
                last_at=ensure_utc(doc["last_at"]),
            )
            for doc in docs
        }

    async def calls(self, *, limit: int, user_id: int | str | None = None) -> list[Call]:
        check_user_id(user_id)
        await ensure_schema(self._db)
        cursor = (
            self._db["llmbroker_calls"]
            .find({"user_id": user_id})
            .sort("called_at", -1)
            .limit(limit)
        )
        docs = await cursor.to_list(length=None)
        return [_call_from_doc(d) for d in docs]

    async def purge_calls(self, *, before: datetime) -> int:
        """Delete all calls older than *before*, across all users. Admin operation."""
        await ensure_schema(self._db)
        result = await self._db["llmbroker_calls"].delete_many(
            {"called_at": {"$lt": before}},
        )
        return result.deleted_count

    async def aclose(self) -> None:
        return
