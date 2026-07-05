"""Dispatch a plain string/``Path`` source to a registry/secrets/knowledge triple.

Dispatch is dumb and explicit: ``.toml``/``.json`` -> file registry + env
secrets (``AsyncBroker`` falls back to the ``state/`` sibling default); ``sqlite://``
/ ``.db`` / ``.sqlite`` -> sqlite, one file backing all three ports;
``postgresql://`` / ``mongodb://`` -> by scheme, one driver shared by all three
ports. Anything else raises a clear error naming the accepted forms.

Each backend package is imported lazily here (never at module load) so a bare
``import llmbroker`` never pulls in a driver package.
"""

from pathlib import Path

from llmbroker.backends.ports import StoreKnowledge, StoreRegistry, StoreSecrets
from llmbroker.protocols.knowledge import KnowledgeProtocol
from llmbroker.protocols.registry import RegistryProtocol
from llmbroker.protocols.secrets import SecretsProtocol
from llmbroker.standalone.registry import Registry as FileRegistry
from llmbroker.standalone.secrets import Secrets as EnvSecrets

_SQLITE_SUFFIXES = (".db", ".sqlite")
_FILE_SUFFIXES = (".toml", ".json")


def resolve_source(
    source: str | Path,
) -> tuple[RegistryProtocol, SecretsProtocol, KnowledgeProtocol | None]:
    """Returns ``(registry, secrets, knowledge)``; a ``None`` knowledge means
    "use the caller's own default" (the file-registry ``state/`` sibling)."""
    source = str(source)
    if source.endswith(_FILE_SUFFIXES):
        return FileRegistry(source), EnvSecrets(), None

    sqlite_path = source.removeprefix("sqlite://")
    if source.startswith("sqlite://") or source.endswith(_SQLITE_SUFFIXES):
        try:
            from llmbroker.sqlite.driver import SqliteDriver  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                f"sqlite source {source!r} requires: pip install llmbroker[sqlite]",
            ) from exc
        driver = SqliteDriver(sqlite_path)
        return StoreRegistry(driver), StoreSecrets(driver), StoreKnowledge(driver)

    if source.startswith("postgresql://"):
        try:
            from llmbroker.postgres.driver import PostgresDriver  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                f"postgres source {source!r} requires: pip install llmbroker[postgres]",
            ) from exc
        driver = PostgresDriver(dsn=source)
        return StoreRegistry(driver), StoreSecrets(driver), StoreKnowledge(driver)

    if source.startswith("mongodb://"):
        try:
            from motor.motor_asyncio import AsyncIOMotorClient  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                f"mongodb source {source!r} requires: pip install llmbroker[mongodb]",
            ) from exc
        from llmbroker.mongodb.driver import MongoDriver  # noqa: PLC0415

        client = AsyncIOMotorClient(source)
        driver = MongoDriver(client.get_default_database(), client=client)
        return StoreRegistry(driver), StoreSecrets(driver), StoreKnowledge(driver)

    raise ValueError(
        f"unrecognized registry source {source!r} — expected a .toml/.json file path,"
        " a sqlite path/URL (.db, .sqlite, sqlite://...), or a postgresql://... /"
        " mongodb://... URL",
    )
