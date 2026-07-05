"""MongoDB-backed mutable registry over ``llmbroker_registry`` — a pure preset mirror."""

from motor.motor_asyncio import AsyncIOMotorDatabase

from llmbroker.backends.ports import DriverRegistry
from llmbroker.mongodb.driver import MongoDriver


class Registry(DriverRegistry):
    """MongoDB-backed mutable registry over ``llmbroker_registry`` — a pure preset mirror."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        super().__init__(MongoDriver(db))
