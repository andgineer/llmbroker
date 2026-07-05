"""MongoDB-backed mutable registry over ``llmbroker_registry`` — a pure preset mirror."""

from motor.motor_asyncio import AsyncIOMotorDatabase

from llmbroker.models import LLMConfig, check_user_id
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
    """MongoDB-backed mutable registry over ``llmbroker_registry`` — a pure preset mirror."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db

    async def load(self, user_id: int | str | None = None) -> list[LLMConfig]:
        check_user_id(user_id)
        await ensure_schema(self._db)
        cursor = self._db["llmbroker_registry"].find({"user_id": user_id}).sort("name", 1)
        docs = await cursor.to_list(length=None)
        return [_config_from_doc(d) for d in docs]

    async def mirror(self, configs: list[LLMConfig], user_id: int | str | None = None) -> None:
        """Total mirror: add new, update existing, delete stored entries absent
        from ``configs`` — the only registry write path."""
        check_user_id(user_id)
        await ensure_schema(self._db)
        source_names = {c.name for c in configs}
        existing_docs = (
            await self._db["llmbroker_registry"]
            .find(
                {"user_id": user_id},
                {"name": 1},
            )
            .to_list(length=None)
        )
        existing_names = {d["name"] for d in existing_docs}

        stale = existing_names - source_names
        if stale:
            await self._db["llmbroker_registry"].delete_many(
                {"user_id": user_id, "name": {"$in": list(stale)}},
            )
        for cfg in configs:
            await self._db["llmbroker_registry"].update_one(
                {"name": cfg.name, "user_id": user_id},
                {
                    "$set": {
                        "base_url": cfg.base_url,
                        "model": cfg.model,
                        "api_key_ref": cfg.api_key_ref,
                        "metadata": cfg.to_metadata(),
                    },
                },
                upsert=True,
            )

    async def aclose(self) -> None:
        return
