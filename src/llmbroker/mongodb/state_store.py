"""MongoDB-backed state store over ``llmbroker_state`` and ``llmbroker_summaries``."""

from dataclasses import replace
from datetime import UTC, datetime

from motor.motor_asyncio import AsyncIOMotorDatabase

from llmbroker.models import LifecyclePhase, LLMState, QualitySummary, check_user_id, reconcile
from llmbroker.mongodb.schema import ensure_schema, ensure_utc

_IDENTITY_KEYS = frozenset({"_id", "llm_name", "user_id"})


class StateStore:
    """MongoDB-backed state store over ``llmbroker_state``."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db

    async def read(self, user_id: int | str | None = None) -> dict[str, LLMState]:
        check_user_id(user_id)
        await ensure_schema(self._db)
        cursor = self._db["llmbroker_state"].find({"user_id": user_id})
        docs = await cursor.to_list(length=None)
        now = datetime.now(UTC)
        result: dict[str, LLMState] = {}
        for doc in docs:
            name = str(doc["llm_name"])
            payload = {k: v for k, v in doc.items() if k not in _IDENTITY_KEYS}
            cooldown = payload.get("cooldown_until")
            if isinstance(cooldown, datetime):
                # Pre-existing docs store a native BSON date, which pymongo
                # returns as naive (the client is never opened tz_aware).
                utc_cooldown = ensure_utc(cooldown)
                if utc_cooldown is not None:
                    payload["cooldown_until"] = utc_cooldown.isoformat()
            result[name] = reconcile(LLMState.from_dict(payload), now)
        return result

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
        await ensure_schema(self._db)
        doc = {"llm_name": name, "user_id": user_id, **payload}
        await self._db["llmbroker_state"].replace_one(
            {"llm_name": name, "user_id": user_id},
            doc,
            upsert=True,
        )

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
        """One atomic aggregation-pipeline update — plain ``$mul``+``$inc`` can't fuse
        multiply-then-add on the same field, so the fold is expressed as a pipeline
        stage that reads the document's own current value via ``$ifNull``."""
        check_user_id(user_id)
        await ensure_schema(self._db)
        decay_pow_sq = decay_pow * decay_pow
        await self._db["llmbroker_summaries"].update_one(
            {"name": name, "operation": operation, "kind": kind, "user_id": user_id},
            [
                {
                    "$set": {
                        "name": name,
                        "operation": operation,
                        "kind": kind,
                        "user_id": user_id,
                        "weight": {
                            "$add": [
                                {"$multiply": [{"$ifNull": ["$weight", 0.0]}, decay_pow]},
                                add_weight,
                            ],
                        },
                        "weighted_good": {
                            "$add": [
                                {"$multiply": [{"$ifNull": ["$weighted_good", 0.0]}, decay_pow]},
                                add_good,
                            ],
                        },
                        "weight_sq": {
                            "$add": [
                                {"$multiply": [{"$ifNull": ["$weight_sq", 0.0]}, decay_pow_sq]},
                                add_weight_sq,
                            ],
                        },
                        "count": {"$add": [{"$ifNull": ["$count", 0]}, add_count]},
                    },
                },
            ],
            upsert=True,
        )

    async def read_summaries(
        self,
        user_id: int | str | None = None,
    ) -> dict[tuple[str, str | None, str], QualitySummary]:
        check_user_id(user_id)
        await ensure_schema(self._db)
        cursor = self._db["llmbroker_summaries"].find({"user_id": user_id})
        docs = await cursor.to_list(length=None)
        return {
            (doc["name"], doc["operation"], doc["kind"]): QualitySummary(
                weight=doc["weight"],
                weighted_good=doc["weighted_good"],
                weight_sq=doc["weight_sq"],
                count=doc["count"],
            )
            for doc in docs
        }

    async def seed_summary(
        self,
        name: str,
        operation: str | None,
        kind: str,
        summary: QualitySummary,
        user_id: int | str | None = None,
    ) -> None:
        check_user_id(user_id)
        await ensure_schema(self._db)
        await self._db["llmbroker_summaries"].update_one(
            {"name": name, "operation": operation, "kind": kind, "user_id": user_id},
            {
                "$setOnInsert": {
                    "weight": summary.weight,
                    "weighted_good": summary.weighted_good,
                    "weight_sq": summary.weight_sq,
                    "count": summary.count,
                },
            },
            upsert=True,
        )

    async def aclose(self) -> None:
        return
