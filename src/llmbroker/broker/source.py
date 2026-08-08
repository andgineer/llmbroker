"""Which ports a source becomes: dispatch of a string/``Path``, and the defaults
each port falls back to when the caller named none.

Dispatch is dumb and explicit: ``.toml``/``.json`` -> file registry + env
secrets with the config file's sibling ``.env`` as fallback (the ``store/``
sibling default below completes it); ``sqlite://``
/ ``.db`` / ``.sqlite`` -> sqlite, one file backing all three ports;
``postgresql://`` / ``mongodb://`` -> by scheme, one driver shared by all three
ports. Anything else raises a clear error naming the accepted forms.

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
_FILE_SUFFIXES = (".toml", ".json")


def resolve_source(
    source: str | Path,
) -> tuple[RegistryProtocol, SecretsProtocol, StoreProtocol | None]:
    """Returns ``(registry, secrets, store)``; a ``None`` store means
    "use the caller's own default" (the file-registry ``store/`` sibling)."""
    source = str(source)
    if source.endswith(_FILE_SUFFIXES):
        # The config file's own directory is where the quickstart's
        # `llmbroker env llms.toml > .env` puts the keys.
        return FileRegistry(source), EnvSecrets(Path(source).parent / ".env"), None

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
        f"unrecognized registry source {source!r} — expected a .toml/.json file path,"
        " a sqlite path/URL (.db, .sqlite, sqlite://...), or a postgresql://... /"
        " mongodb://... URL",
    )


def default_secrets(registry: RegistryProtocol) -> SecretsProtocol:
    """A file/TOML registry resolves keys from the environment with its own
    sibling ``.env`` as fallback — the file ``llmbroker env`` writes. Any other
    registry gets the plain environment resolver."""
    if isinstance(registry, FileRegistry):
        return EnvSecrets(registry.path.parent / ".env")
    return EnvSecrets()


def default_store(registry: RegistryProtocol) -> StoreProtocol:
    """A file/TOML registry gets a ``store/`` dir sibling to its config file;
    any other registry (a bare DB registry, a custom object) falls back to
    ``./store`` under the CWD — not an error, just an unopinionated default."""
    if isinstance(registry, FileRegistry):
        return FileStore(registry.path.parent / "store")
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


def file_target_path(registry: FileRegistry) -> Path:
    """A file registry is rewritten as TOML, so only a ``.toml`` config can be synced."""
    if registry.path.suffix.lower() != ".toml":
        raise ValueError(
            f"registry {registry.path} cannot be synced: a file registry is rewritten as"
            " TOML, so only a .toml config is a sync target — convert it, or move the"
            " lineup into a database registry",
        )
    return registry.path
