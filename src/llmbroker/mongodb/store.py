"""MongoDB-backed queryable store over ``llmbroker_calls`` + the
``llmbroker_disabled`` admin verdict map."""

from datetime import timedelta

from motor.motor_asyncio import AsyncIOMotorDatabase

from llmbroker.backends.ports import DriverStore
from llmbroker.journal_policy import RETENTION_DEFAULT
from llmbroker.mongodb.driver import MongoDriver


class Store(DriverStore):
    """MongoDB-backed queryable store over ``llmbroker_calls`` + the
    ``llmbroker_disabled`` admin verdict map."""

    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        *,
        retention: timedelta = RETENTION_DEFAULT,
    ) -> None:
        super().__init__(MongoDriver(db), retention=retention)
