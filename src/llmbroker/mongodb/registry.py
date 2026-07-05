"""MongoDB-backed mutable registry over ``llmbroker_registry`` — a pure preset mirror."""

from motor.motor_asyncio import AsyncIOMotorDatabase

from llmbroker.backends.ports import StoreRegistry
from llmbroker.mongodb.driver import MongoDriver


class Registry(StoreRegistry):
    """MongoDB-backed mutable registry over ``llmbroker_registry`` — a pure preset mirror."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        super().__init__(MongoDriver(db))
