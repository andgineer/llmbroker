"""MongoDB-backed mutable secrets store over ``llmbroker_secrets``."""

from motor.motor_asyncio import AsyncIOMotorDatabase

from llmbroker.backends.ports import StoreSecrets
from llmbroker.mongodb.driver import MongoDriver


class Secrets(StoreSecrets):
    """MongoDB-backed mutable secrets store over ``llmbroker_secrets``."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        super().__init__(MongoDriver(db))
