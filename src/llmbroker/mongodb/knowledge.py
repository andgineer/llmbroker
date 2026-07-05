"""MongoDB-backed queryable knowledge store over ``llmbroker_calls`` + the
``llmbroker_disabled`` admin verdict map."""

from datetime import timedelta

from motor.motor_asyncio import AsyncIOMotorDatabase

from llmbroker.backends.ports import StoreKnowledge
from llmbroker.mongodb.driver import MongoDriver

_DEFAULT_RETENTION = timedelta(days=90)


class Knowledge(StoreKnowledge):
    """MongoDB-backed queryable knowledge store over ``llmbroker_calls`` + the
    ``llmbroker_disabled`` admin verdict map."""

    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        *,
        retention: timedelta = _DEFAULT_RETENTION,
    ) -> None:
        super().__init__(MongoDriver(db), retention=retention)
