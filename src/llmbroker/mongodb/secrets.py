"""MongoDB-backed mutable secrets store over ``llmbroker_secrets``."""

from motor.motor_asyncio import AsyncIOMotorDatabase

from llmbroker.backends.ports import DriverSecrets
from llmbroker.mongodb.driver import MongoDriver


class Secrets(DriverSecrets):
    """MongoDB-backed mutable secrets store over ``llmbroker_secrets``."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        super().__init__(MongoDriver(db))
