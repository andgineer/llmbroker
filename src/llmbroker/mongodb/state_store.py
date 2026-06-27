"""MongoDB-backed state store over ``llmbroker_state``."""

from datetime import UTC, datetime

from motor.motor_asyncio import AsyncIOMotorDatabase

from llmbroker.models import LifecyclePhase, LLMState, check_user_id
from llmbroker.mongodb.schema import ensure_schema, ensure_utc

_TRUST_STORED_PHASES = frozenset({LifecyclePhase.OFFLINE, LifecyclePhase.PROBING})


class StateStore:
    """MongoDB-backed state store over ``llmbroker_state``."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db

    async def read(self, user_id: int | str | None = None) -> dict[str, LLMState]:
        check_user_id(user_id)
        await ensure_schema(self._db)
        cursor = self._db["llmbroker_state"].find({"user_id": user_id})
        docs = await cursor.to_list(length=None)
        result: dict[str, LLMState] = {}
        now = datetime.now(UTC)
        for doc in docs:
            name = str(doc["llm_name"])
            stored_phase = LifecyclePhase(doc["phase"])
            cooldown_until = ensure_utc(doc.get("cooldown_until"))
            fail_count = int(doc["fail_count"])
            if stored_phase in _TRUST_STORED_PHASES:
                phase = stored_phase
                if cooldown_until is not None and cooldown_until <= now:
                    cooldown_until = None
            elif cooldown_until is not None and cooldown_until > now:
                phase = LifecyclePhase.COOLING
            elif stored_phase in {LifecyclePhase.AVAILABLE, LifecyclePhase.COOLING}:
                phase = LifecyclePhase.AVAILABLE
                cooldown_until = None
            else:
                raise ValueError(
                    f"Unexpected stored phase {stored_phase!r}: "
                    "add it to _TRUST_STORED_PHASES or handle it explicitly",
                )
            result[name] = LLMState(
                phase=phase,
                cooldown_until=cooldown_until,
                fail_count=fail_count,
            )
        return result

    async def write(self, name: str, state: LLMState, user_id: int | str | None = None) -> None:
        check_user_id(user_id)
        if state.cooldown_until is not None and state.cooldown_until.tzinfo is None:
            raise ValueError("LLMState.cooldown_until must be timezone-aware")
        cooldown = (
            state.cooldown_until
            if state.cooldown_until is not None and state.phase is not LifecyclePhase.AVAILABLE
            else None
        )
        await ensure_schema(self._db)
        doc = {
            "llm_name": name,
            "phase": state.phase.value,
            "cooldown_until": cooldown,
            "fail_count": state.fail_count,
            "user_id": user_id,
        }
        await self._db["llmbroker_state"].replace_one(
            {"llm_name": name, "user_id": user_id},
            doc,
            upsert=True,
        )

    async def aclose(self) -> None:
        return
