"""MongoDB-backed mutable registry over ``llmbroker_registry``."""

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from llmbroker.models import LLMConfig, LLMProfile, check_user_id
from llmbroker.mongodb.schema import ensure_schema


def _config_from_doc(doc: dict) -> LLMConfig:
    return LLMConfig.from_metadata(
        name=doc["name"],
        base_url=doc["base_url"],
        model=doc["model"],
        api_key_ref=doc["api_key_ref"],
        metadata=doc.get("metadata"),
    )


class Registry:
    """MongoDB-backed mutable registry over ``llmbroker_registry``."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db

    async def load(self, user_id: int | str | None = None) -> list[LLMConfig]:
        check_user_id(user_id)
        await ensure_schema(self._db)
        cursor = self._db["llmbroker_registry"].find({"user_id": user_id}).sort("name", 1)
        docs = await cursor.to_list(length=None)
        return [_config_from_doc(d) for d in docs]

    async def get(self, name: str, user_id: int | str | None = None) -> LLMConfig | None:
        check_user_id(user_id)
        await ensure_schema(self._db)
        doc = await self._db["llmbroker_registry"].find_one({"name": name, "user_id": user_id})
        if doc is None:
            return None
        return _config_from_doc(doc)

    async def add(self, cfg: LLMConfig, user_id: int | str | None = None) -> None:
        check_user_id(user_id)
        await ensure_schema(self._db)
        doc = {
            "name": cfg.name,
            "base_url": cfg.base_url,
            "model": cfg.model,
            "api_key_ref": cfg.api_key_ref,
            "metadata": cfg.to_metadata(),
            "user_id": user_id,
        }
        try:
            await self._db["llmbroker_registry"].insert_one(doc)
        except DuplicateKeyError:
            raise ValueError(f"LLM {cfg.name!r} already exists") from None

    async def update(self, cfg: LLMConfig, user_id: int | str | None = None) -> None:
        """Update only the static fields — must never touch ``profile`` (learned data)."""
        check_user_id(user_id)
        await ensure_schema(self._db)
        result = await self._db["llmbroker_registry"].update_one(
            {"name": cfg.name, "user_id": user_id},
            {
                "$set": {
                    "base_url": cfg.base_url,
                    "model": cfg.model,
                    "api_key_ref": cfg.api_key_ref,
                    "metadata": cfg.to_metadata(),
                },
            },
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

    async def read_profiles(self, user_id: int | str | None = None) -> dict[str, LLMProfile]:
        check_user_id(user_id)
        await ensure_schema(self._db)
        cursor = self._db["llmbroker_registry"].find({"user_id": user_id})
        docs = await cursor.to_list(length=None)
        return {d["name"]: LLMProfile.from_dict(d.get("profile") or {}) for d in docs}

    async def write_profile(
        self,
        name: str,
        profile: LLMProfile,
        user_id: int | str | None = None,
    ) -> None:
        check_user_id(user_id)
        await ensure_schema(self._db)
        result = await self._db["llmbroker_registry"].update_one(
            {"name": name, "user_id": user_id},
            {"$set": {"profile": profile.to_dict()}},
        )
        if result.matched_count == 0:
            raise KeyError(name)

    async def aclose(self) -> None:
        return
