"""Catalog: keep the live pool's membership in sync with the registry. ``apply`` is
the only registry write path; what it writes is decided in ``broker.merge``."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence

from llmbroker.broker.pool import LLMPool
from llmbroker.exceptions import EmptyRegistryError, PoolModelError, UnknownModelError
from llmbroker.models import (
    DeclaredModels,
    KeyInfo,
    LLMConfig,
    PendingKey,
    PoolHealth,
    check_aliases,
)
from llmbroker.protocols.registry import (
    KeyInfoProtocol,
    MutableRegistryProtocol,
    RegistryProtocol,
)
from llmbroker.protocols.secrets import MutableSecretsProtocol, SecretsProtocol
from llmbroker.standalone.secrets import Secrets

logger = logging.getLogger("llmbroker.broker")

# Every provider count that is not degraded is one state. Both sentinels are
# negative, so a non-negative remembered state is exactly "was degraded".
_HEALTHY = -1
_NO_POOL = -2


async def _resolve_non_empty(secrets: SecretsProtocol, ref: str) -> str | None:
    """One backend lookup, with a blank value read as no value at all — any
    backend can hand one back, and key presence authorizes preset removals."""
    try:
        value = await secrets.resolve(ref)
    except KeyError:
        return None
    return value if value.strip() else None


async def resolve_key(
    secrets: SecretsProtocol,
    api_key_ref: str,
    scope: str | None,
) -> str | None:
    """Resolve ``api_key_ref`` to a key: scope-prefixed (own) ref first, then shared."""
    if scope is not None:
        own = await _resolve_non_empty(secrets, f"{scope}/{api_key_ref}")
        if own is not None:
            return own
    return await _resolve_non_empty(secrets, api_key_ref)


def check_overlay(stored: list[LLMConfig], declared: list[LLMConfig]) -> None:
    """A model declared in code must not claim a handle the registry already uses.

    Two sources for one name is the case a registry's own uniqueness rules cannot
    see, so it is named here — with both sides, since the only fix is to drop one.
    """
    names = {c.name for c in stored}
    aliases = {c.alias for c in stored if c.alias is not None}
    seen_names: set[str] = set()
    seen_aliases: set[str] = set()
    for cfg in declared:
        # The alias first: it is the word the caller actually typed, while a name
        # resolved from it carries a model version they never saw.
        if cfg.alias is not None and cfg.alias in seen_aliases:
            raise ValueError(
                f"direct= declares the alias {cfg.alias!r} twice — an alias names"
                " exactly one entry",
            )
        if cfg.name in seen_names:
            raise ValueError(
                f"direct= declares {cfg.name!r} twice — drop one of the two entries",
            )
        if cfg.name in names:
            raise ValueError(
                f"direct= declares {cfg.name!r}, and the registry already carries an"
                " entry of that name — drop one of the two declarations",
            )
        if cfg.alias is not None:
            if cfg.alias in aliases:
                raise ValueError(
                    f"direct= declares the alias {cfg.alias!r}, and the registry already"
                    " carries an entry with it — an alias names exactly one entry",
                )
            seen_aliases.add(cfg.alias)
        seen_names.add(cfg.name)


_POOL_MODEL_HINT = (
    "pool models are anonymous — reach them with ask()/chat()/stream(), which route and"
    " learn; add a [[custom]] entry for the model if you need to call it by name"
)


def find_custom(configs: list[LLMConfig], alias: str | None, name: str | None) -> LLMConfig:
    """Resolve one user-owned entry from exactly one of the two keyspaces.

    A miss whose string exists in the *other* keyspace says so, since the two are
    one typo apart at a call site.
    """
    if alias is not None:
        for cfg in configs:
            if cfg.custom and cfg.alias == alias:
                return cfg
        if any(c.custom and c.name == alias for c in configs):
            raise UnknownModelError(
                f"no entry with alias {alias!r}; an entry with this name exists"
                f" — call direct(name={alias!r})",
            )
        if any(c.name == alias for c in configs):
            # The pre-alias call shape: direct() took a name, and a pool name at that.
            # Sending it to direct(name=...) first would only spend an error saying so.
            raise PoolModelError(f"{alias!r} is a preset-managed pool model: {_POOL_MODEL_HINT}")
        raise UnknownModelError(f"no entry with alias {alias!r} in the registry")
    for cfg in configs:
        if cfg.custom and cfg.name == name:
            return cfg
    if any(c.custom and c.alias == name for c in configs):
        raise UnknownModelError(
            f"no entry named {name!r}; an entry with this alias exists — call direct({name!r})",
        )
    if any(c.name == name for c in configs):
        raise PoolModelError(f"{name!r} is a preset-managed pool model: {_POOL_MODEL_HINT}")
    raise UnknownModelError(f"no model named {name!r} in the registry")


class Catalog:
    """Reconciles the persistent registry into the live pool, and mirrors presets into it."""

    def __init__(
        self,
        registry: RegistryProtocol,
        secrets: SecretsProtocol,
        pool: LLMPool,
        *,
        scope: str | None,
        overlay: "Callable[[], Awaitable[DeclaredModels]] | None" = None,
    ) -> None:
        self._registry = registry
        self._secrets = secrets
        self._pool = pool
        self._scope = scope
        self._overlay = overlay
        self._declared: DeclaredModels | None = None
        self._declared_lock = asyncio.Lock()
        self._health = PoolHealth()
        self._direct_missing_keys: tuple[PendingKey, ...] = ()
        self._key_info: dict[str, KeyInfo] = {}
        self._reported_state: int | None = None
        self._reported_missing: set[str] = set()

    @property
    def health(self) -> PoolHealth:
        """The pool-wide counts from the last reconcile — the same numbers the
        degradation alarm uses, so log and admin UI cannot diverge."""
        return self._health

    @property
    def direct_missing_keys(self) -> tuple[PendingKey, ...]:
        """Refs the host's own ``direct``-reachable entries want and cannot resolve."""
        return self._direct_missing_keys

    def key_help(self, ref: str) -> str:
        """Where this ref's key comes from: the registry's own ``[keys]`` first —
        a host that wrote its own hint means it — then the paid catalog's."""
        stored = self._key_info[ref].help if ref in self._key_info else ""
        if stored:
            return stored
        return self._declared.key_help.get(ref, "") if self._declared is not None else ""

    def invalidate_declared(self) -> None:
        """Drop the resolved overlay so the next read follows the catalog again."""
        self._declared = None

    async def entries(self) -> list[LLMConfig]:
        """The stored lineup with the models declared in code overlaid. This read is
        not the alias clock — ``direct()`` comes through here on every call and must
        not pay a catalog parse."""
        configs = await self._registry.load()
        if self._overlay is None:
            return configs
        declared = await self._resolve_overlay()
        check_overlay(configs, declared.configs)
        return [*configs, *declared.configs]

    async def _resolve_overlay(self) -> DeclaredModels:
        """Resolve ``direct=`` once, however many callers arrive together: the callers
        that find the resolution dropped are requests, and unserialized each would
        parse — or where nothing is writable, fetch — the catalog for itself."""
        if self._declared is not None:
            return self._declared
        async with self._declared_lock:
            if self._declared is None and self._overlay is not None:
                declared = await self._overlay()
                # The same bootstrap `sync` gives the stored lineup, without which a
                # declared model is dead wherever secrets are not the environment.
                await self.seed_secrets(declared.configs)
                self._declared = declared
            return self._declared if self._declared is not None else DeclaredModels()

    async def provision(self) -> None:
        """Reconcile the pool with the registry. The caller serializes this
        one-time init; it is not re-entrant. Raises when there is nothing at all."""
        configs = await self.entries()
        if not configs:
            raise EmptyRegistryError(
                "registry is empty — no lineup is stored and none could be fetched."
                ' Fill it with `await broker.sync("freetier")`, or from a deploy job,'
                " or check network access to the preset catalog",
            )
        await self._reconcile(configs)

    async def resync(self) -> None:
        """Re-read the registry and reconcile pool membership — no emptiness check.

        Called by the debounced journal rebuild so registry edits and key changes
        from other processes/nodes take effect on a running broker.
        """
        await self._reconcile(await self.entries())

    async def _reconcile(self, configs: list[LLMConfig]) -> None:
        """Reconcile the managed entries into the live pool. A custom entry stays
        in the registry (reachable via ``broker.direct``) and never joins it."""
        managed = [c for c in configs if not c.custom]
        # Here, not in a registry: a host may implement the port itself, and a refresh
        # would re-point a pooled alias at whatever the paid catalog recommends.
        check_aliases(managed)
        names = {c.name for c in managed}
        for name in list(self._pool.configs):
            if name not in names:
                await self._pool.drop(name)
        for order, cfg in enumerate(managed):
            await self._pool.add(cfg, await self._resolve_key(cfg), order=order)
        await self._measure(managed, [c for c in configs if c.custom])
        self._report_health()
        self._report_missing_keys()

    async def _measure(self, managed: list[LLMConfig], direct: list[LLMConfig]) -> None:
        usable: set[str] = set()
        missing: dict[str, list[str]] = {}
        total: set[str] = set()
        for cfg in managed:
            if not cfg.api_key_ref:
                continue
            total.add(cfg.api_key_ref)
            if self._pool.has_key(cfg.name):
                usable.add(cfg.api_key_ref)
            else:
                missing.setdefault(cfg.api_key_ref, []).append(cfg.name)
        held_back = {ref: names for ref, names in missing.items() if ref not in usable}
        direct_held = await self._direct_without_keys(direct)
        # Help text is read only for a ref that is missing, so a fully-keyed
        # installation — the common case — costs no registry read at all.
        if (held_back or direct_held) and isinstance(self._registry, KeyInfoProtocol):
            self._key_info = await self._registry.key_info()
        self._health = PoolHealth(
            providers_usable=len(usable),
            providers_total=len(total),
            missing_keys=self._pending(held_back),
        )
        self._direct_missing_keys = self._pending(direct_held)

    async def _direct_without_keys(self, direct: list[LLMConfig]) -> dict[str, list[str]]:
        """Which refs the host's own entries want and cannot resolve, named by the
        handle ``direct()`` takes — the alias where there is one, since a resolved
        ``name`` carries a version the caller never typed."""
        missing: dict[str, list[str]] = {}
        for cfg in direct:
            if not cfg.api_key_ref:
                continue
            if await resolve_key(self._secrets, cfg.api_key_ref, self._scope) is None:
                missing.setdefault(cfg.api_key_ref, []).append(cfg.alias or cfg.name)
        return missing

    def _pending(self, refs: dict[str, list[str]]) -> tuple[PendingKey, ...]:
        return tuple(
            PendingKey(api_key_ref=ref, help=self.key_help(ref), entry_names=tuple(names))
            for ref, names in refs.items()
        )

    def _report_health(self) -> None:
        """One line per transition only: a healthy log carries none of these, and a
        broken one carries exactly one per change."""
        health = self._health
        # Keyed on the state, not on the log level: 0 and 1 usable providers are
        # both ERROR but different states, and the 1 -> 0 step is the outage.
        if health.providers_total == 0:
            state = _NO_POOL
        else:
            state = health.providers_usable if health.degraded else _HEALTHY
        previous = self._reported_state
        if state == previous:
            return
        self._reported_state = state
        refs = ", ".join(k.api_key_ref for k in health.missing_keys)
        tail = f" — no key for {refs}" if refs else ""
        if state == _NO_POOL:
            # No managed entry names a provider: there is no pool to degrade, and
            # "no provider has a key" would name a cause that is not the case.
            return
        if health.providers_usable == 0:
            logger.error("pool cannot serve any request: no provider has a key%s", tail)
        elif health.degraded:
            logger.error(
                "pool degraded, no failover left: 1 of %d providers usable%s",
                health.providers_total,
                tail,
            )
        elif previous is not None and previous >= 0:
            logger.info(
                "pool recovered: %d of %d providers usable",
                health.providers_usable,
                health.providers_total,
            )

    async def apply(self, configs: list[LLMConfig]) -> None:
        """Mirror an already-merged lineup into the registry and seed its keys.

        The merge decision — what the lineup should be — belongs to
        ``broker.merge``; this half only writes it.
        """
        registry = self._require_mutable_registry()
        await registry.mirror(configs)
        await self.seed_secrets(configs)

    def _report_missing_keys(self) -> None:
        """One line per ref the first time it turns up missing, carrying where to get
        it. Deduplicated on the set, not on a clock: a reconcile runs on every minute
        of activity and a key that stays missing must not fill the log."""
        pending = (*self._health.missing_keys, *self._direct_missing_keys)
        pooled_refs = {k.api_key_ref for k in self._health.missing_keys}
        for key in pending:
            if key.api_key_ref in self._reported_missing:
                continue
            self._reported_missing.add(key.api_key_ref)
            tail = f" — {key.help}" if key.help else ""
            names = ", ".join(key.entry_names)
            if key.api_key_ref in pooled_refs:
                logger.info(
                    "api_key_ref %r not resolved — %s inactive until the env var / secret"
                    " is set; this is normal, the pool routes over whatever keys are"
                    " present%s",
                    key.api_key_ref,
                    names,
                    tail,
                )
            else:
                logger.info(
                    "api_key_ref %r not resolved — %s is reached by name only, so"
                    " direct() on it fails until the env var / secret is set%s",
                    key.api_key_ref,
                    names,
                    tail,
                )
        self._reported_missing &= {k.api_key_ref for k in pending}

    async def _resolve_key(self, cfg: LLMConfig) -> str | None:
        return await resolve_key(self._secrets, cfg.api_key_ref, self._scope)

    async def seed_secrets(self, configs: Sequence[LLMConfig]) -> None:
        """Copy any env-resolvable keys into a mutable secrets backend, preserving existing."""
        if not isinstance(self._secrets, MutableSecretsProtocol):
            return
        bootstrap = Secrets()
        for cfg in configs:
            if await _resolve_non_empty(self._secrets, cfg.api_key_ref) is not None:
                continue  # already resolvable — preserve
            value = await _resolve_non_empty(bootstrap, cfg.api_key_ref)
            if value is not None:
                await self._secrets.set(cfg.api_key_ref, value)

    def _require_mutable_registry(self) -> MutableRegistryProtocol:
        if not isinstance(self._registry, MutableRegistryProtocol):
            raise TypeError(
                f"{type(self._registry).__name__} does not support mutations"
                " (sync requires a mutable registry such as llmbroker.sqlite.Registry)",
            )
        return self._registry
