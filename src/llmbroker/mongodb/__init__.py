"""MongoDB backend: registry, knowledge store, and secrets.

Needs the ``motor`` driver (``llmbroker[mongodb]``). All collections are
``llmbroker_``-prefixed and owned by ``ensure_schema``.
"""

from datetime import timedelta

from motor.motor_asyncio import AsyncIOMotorDatabase

from llmbroker.backends.ports import StoreKnowledge, StoreRegistry, StoreSecrets
from llmbroker.mongodb.driver import MongoDriver

__all__ = ["Knowledge", "Registry", "Secrets"]

_DEFAULT_RETENTION = timedelta(days=90)


class Registry(StoreRegistry):
    """MongoDB-backed mutable registry over ``llmbroker_registry`` — a pure preset mirror."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        super().__init__(MongoDriver(db))


class Secrets(StoreSecrets):
    """MongoDB-backed mutable secrets store over ``llmbroker_secrets``."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        super().__init__(MongoDriver(db))


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
