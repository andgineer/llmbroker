"""Which ports a source becomes: dispatch of a string/``Path``, and the defaults
each port falls back to when the caller named none.

Dispatch is dumb and explicit: ``sqlite://`` / ``.db`` / ``.sqlite`` -> sqlite,
one file backing all three ports; ``postgresql://`` / ``mongodb://`` -> by
scheme, one driver shared by all three ports. Anything else raises a clear error
naming the accepted forms.

Each backend package is imported lazily here (never at module load) so a bare
``import llmbroker`` never pulls in a driver package.
"""

from pathlib import Path

from llmbroker.backends.inmemory import InMemoryDriver
from llmbroker.backends.ports import DriverRegistry, DriverSecrets, DriverStore
from llmbroker.protocols.registry import RegistryProtocol
from llmbroker.protocols.secrets import SecretsProtocol
from llmbroker.protocols.store import StoreProtocol
from llmbroker.standalone.registry import Registry as FileRegistry
from llmbroker.standalone.secrets import Secrets as EnvSecrets
from llmbroker.standalone.store import FileStore, InMemoryStore

_SQLITE_SUFFIXES = (".db", ".sqlite")


def resolve_source(
    source: str | Path,
) -> tuple[RegistryProtocol, SecretsProtocol, StoreProtocol | None]:
    """Returns ``(registry, secrets, store)``; a ``None`` store means
    "use the caller's own default"."""
    source = str(source)
    sqlite_path = source.removeprefix("sqlite://")
    if source.startswith("sqlite://") or source.endswith(_SQLITE_SUFFIXES):
        try:
            from llmbroker.sqlite.driver import SqliteDriver  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                f"sqlite source {source!r} requires: pip install llmbroker[sqlite]",
            ) from exc
        driver = SqliteDriver(sqlite_path)
        return DriverRegistry(driver), DriverSecrets(driver), DriverStore(driver)

    if source.startswith("postgresql://"):
        try:
            from llmbroker.postgres.driver import PostgresDriver  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                f"postgres source {source!r} requires: pip install llmbroker[postgres]",
            ) from exc
        driver = PostgresDriver(dsn=source)
        return DriverRegistry(driver), DriverSecrets(driver), DriverStore(driver)

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
        return DriverRegistry(driver), DriverSecrets(driver), DriverStore(driver)

    raise ValueError(
        f"unrecognized registry source {source!r} — expected a sqlite path/URL"
        " (.db, .sqlite, sqlite://...), or a postgresql://... / mongodb://... URL."
        " A lineup is not a file a host names: use Broker() for the curated pool in"
        " llmbroker's own directory, or pass a registry object of your own",
    )


def default_secrets() -> SecretsProtocol:
    """A registry the host brought itself gets the plain environment resolver."""
    return EnvSecrets()


def default_store() -> StoreProtocol:
    """A registry the host brought itself falls back to ``./store`` under the CWD —
    not an error, just an unopinionated default."""
    return FileStore(Path("store"))


def lineup_path(home: Path) -> Path:
    """The lineup file inside llmbroker's own directory — the one a zero-config broker
    keeps its pool in, and the one ``add-model`` adds to."""
    return home / "lineup.toml"


def zero_config_ports(
    home: Path | None,
) -> tuple[RegistryProtocol, SecretsProtocol, StoreProtocol]:
    """The installation a broker builds for itself when given no source at all:
    the curated pool in the home directory, keys from the environment with the
    working directory's ``.env`` behind them, one journal per machine.

    That journal is machine-global on purpose rather than by compromise. Keys here
    come from the environment, so the quota it tracks really is one pool; a journal
    scattered per working directory would make every run rediscover the same 429 and
    pay for it again. Two projects on genuinely different keys are already separated
    by the key hash, and ``home=`` separates everything else.

    Nowhere writable is a supported outcome: the broker then keeps its lineup and
    its journal in memory and simply remembers nothing between runs.
    """
    secrets = EnvSecrets(Path(".env"))
    if home is None:
        return DriverRegistry(InMemoryDriver()), secrets, InMemoryStore()
    return FileRegistry(lineup_path(home)), secrets, FileStore(home / "store")
