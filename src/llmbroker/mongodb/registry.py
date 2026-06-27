"""MongoDB-backed mutable registry over ``llmbroker_registry``."""

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from llmbroker.models import LLMConfig, check_user_id
from llmbroker.mongodb.schema import ensure_schema


class Registry:
    """MongoDB-backed mutable registry over ``llmbroker_registry``."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db

    async def load(self, user_id: int | str | None = None) -> list[LLMConfig]:
        check_user_id(user_id)
        await ensure_schema(self._db)
        cursor = self._db["llmbroker_registry"].find({"user_id": user_id}).sort("name", 1)
        docs = await cursor.to_list(length=None)
        return [
            LLMConfig(
                name=d["name"],
                base_url=d["base_url"],
                model=d["model"],
                api_key_ref=d["api_key_ref"],
            )
            for d in docs
        ]

    async def get(self, name: str, user_id: int | str | None = None) -> LLMConfig | None:
        check_user_id(user_id)
        await ensure_schema(self._db)
        doc = await self._db["llmbroker_registry"].find_one({"name": name, "user_id": user_id})
        if doc is None:
            return None
        return LLMConfig(
            name=doc["name"],
            base_url=doc["base_url"],
            model=doc["model"],
            api_key_ref=doc["api_key_ref"],
        )

    async def add(self, cfg: LLMConfig, user_id: int | str | None = None) -> None:
        check_user_id(user_id)
        await ensure_schema(self._db)
        doc = {
            "name": cfg.name,
            "base_url": cfg.base_url,
            "model": cfg.model,
            "api_key_ref": cfg.api_key_ref,
            "user_id": user_id,
        }
        try:
            await self._db["llmbroker_registry"].insert_one(doc)
        except DuplicateKeyError:
            raise ValueError(f"LLM {cfg.name!r} already exists") from None

    async def update(self, cfg: LLMConfig, user_id: int | str | None = None) -> None:
        check_user_id(user_id)
        await ensure_schema(self._db)
        doc = {
            "name": cfg.name,
            "base_url": cfg.base_url,
            "model": cfg.model,
            "api_key_ref": cfg.api_key_ref,
            "user_id": user_id,
        }
        result = await self._db["llmbroker_registry"].replace_one(
            {"name": cfg.name, "user_id": user_id},
            doc,
        )
        if result.matched_count == 0:
            raise KeyError(cfg.name)

    async def remove(self, name: str, user_id: int | str | None = None) -> None:
        check_user_id(user_id)
        await ensure_schema(self._db)
        result = await self._db["llmbroker_registry"].delete_one(
            {"name": name, "user_id": user_id},
        )
        if result.deleted_count == 0:
            raise KeyError(name)

    async def aclose(self) -> None:
        return
