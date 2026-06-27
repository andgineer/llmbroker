"""MongoDB-backed mutable secrets store over ``llmbroker_secrets``."""

from motor.motor_asyncio import AsyncIOMotorDatabase

from llmbroker.exceptions import UserScopeError
from llmbroker.models import check_user_id
from llmbroker.mongodb.schema import ensure_schema


class Secrets:
    """MongoDB-backed mutable secrets store over ``llmbroker_secrets``."""

    def __init__(self, db: AsyncIOMotorDatabase, *, require_user_id: bool = False) -> None:
        self._db = db
        self._require_user_id = require_user_id

    async def resolve(self, ref: str, user_id: int | str | None = None) -> str:
        check_user_id(user_id)
        if self._require_user_id and user_id is None:
            raise UserScopeError(
                "mongodb.Secrets: user_id is required (require_user_id=True) but received None",
            )
        await ensure_schema(self._db)
        doc = await self._db["llmbroker_secrets"].find_one({"ref": ref, "user_id": user_id})
        if doc is None:
            raise KeyError(f"mongodb.Secrets: ref {ref!r} not found")
        return str(doc["value"])

    async def set(self, ref: str, value: str, user_id: int | str | None = None) -> None:
        check_user_id(user_id)
        if self._require_user_id and user_id is None:
            raise UserScopeError(
                "mongodb.Secrets: user_id is required (require_user_id=True) but received None",
            )
        await ensure_schema(self._db)
        await self._db["llmbroker_secrets"].replace_one(
            {"ref": ref, "user_id": user_id},
            {"ref": ref, "value": value, "user_id": user_id},
            upsert=True,
        )

    async def aclose(self) -> None:
        return
