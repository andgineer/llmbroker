"""Catalog: keep the live pool's membership in sync with the registry. ``apply`` is
the only registry write path; what it writes is decided in ``broker.merge``."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence

from llmbroker.broker.keyring import KeyRing, resolve_ref
from llmbroker.broker.pool import LLMPool
from llmbroker.exceptions import EmptyRegistryError, PoolModelError, UnknownModelError
from llmbroker.models import (
    DeclaredModels,
    KeyInfo,
    LLMConfig,
    PendingKey,
    PoolHealth,
)
from llmbroker.protocols.registry import (
    KeyInfoProtocol,
    MutableRegistryProtocol,
    RegistryProtocol,
)
from llmbroker.protocols.secrets import MutableSecretsProtocol, SecretsProtocol
from llmbroker.protocols.store import DisabledMapProtocol, StoreProtocol
from llmbroker.standalone.secrets import Secrets

logger = logging.getLogger("llmbroker.broker")

# Every provider count that is not degraded is one state. Both sentinels are
# negative, so a non-negative remembered state is exactly "was degraded".
_HEALTHY = -1
_NO_POOL = -2


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
    " learn; declare the model with direct=[...] if you need to call it by name"
)


def find_declared(
    stored: list[LLMConfig],
    declared: list[LLMConfig],
    alias: str | None,
    name: str | None,
) -> LLMConfig:
    """Resolve one model declared with ``direct=`` from exactly one of the two keyspaces.

    A miss whose string exists in the *other* keyspace, or in the pool, says so —
    those are one typo and one wrong expectation apart at a call site.
    """
    if alias is not None:
        for cfg in declared:
            if cfg.alias == alias:
                return cfg
        if any(c.name == alias for c in declared):
            raise UnknownModelError(
                f"no declared model with alias {alias!r}; a declared model with this name"
                f" exists — call direct(name={alias!r})",
            )
        if any(c.name == alias for c in stored):
            # The pre-alias call shape: direct() took a name, and a pool name at that.
            # Sending it to direct(name=...) first would only spend an error saying so.
            raise PoolModelError(f"{alias!r} is a preset-managed pool model: {_POOL_MODEL_HINT}")
        raise UnknownModelError(f"no model was declared with alias {alias!r}")
    for cfg in declared:
        if cfg.name == name:
            return cfg
    if any(c.alias == name for c in declared):
        raise UnknownModelError(
            f"no declared model named {name!r}; a declared model with this alias exists"
            f" — call direct({name!r})",
        )
    if any(c.name == name for c in stored):
        raise PoolModelError(f"{name!r} is a preset-managed pool model: {_POOL_MODEL_HINT}")
    raise UnknownModelError(f"no model named {name!r} was declared with direct=")


_EMPTY_NO_AUTOFILL = (
    "registry is empty and this installation fetches nothing on its own"
    " (sync_interval=None) — fill it from the deploy job that runs your migrations,"
    " with `await broker.sync()`"
)
_EMPTY_NOT_FILLED = (
    "registry is empty — no model list is stored and nothing put one there. Fill it"
    ' with `await broker.sync("freetier")`, or from a deploy job; a broker that follows'
    " nothing (sync=None) fills nothing by itself, and a fill that ran and failed said"
    " why in the log at WARNING"
)


class Catalog:
    """Reconciles the persistent registry into the live pool, and mirrors presets into it."""

    def __init__(  # noqa: PLR0913 - the three ports plus what this pool is allowed to do
        self,
        registry: RegistryProtocol,
        secrets: SecretsProtocol,
        pool: LLMPool,
        ring: KeyRing,
        store: StoreProtocol,
        *,
        overlay: "Callable[[], Awaitable[DeclaredModels]] | None" = None,
        autofill: bool = True,
        relearn: "Callable[[], Awaitable[None]] | None" = None,
    ) -> None:
        self._registry = registry
        self._secrets = secrets
        self._pool = pool
        self._ring = ring
        self._store = store
        self._overlay = overlay
        self._autofill = autofill
        self._relearn = relearn
        self._declared: DeclaredModels | None = None
        self._declared_lock = asyncio.Lock()
        self._health = PoolHealth()
        self._direct_missing_keys: tuple[PendingKey, ...] = ()
        self._key_info: dict[str, KeyInfo] = {}
        self._payable: frozenset[str] = frozenset()
        self._scoped_refs: frozenset[str] = frozenset()
        self._empty = False
        self._reported_state: int | None = None
        self._reported_missing: set[str] = set()

    @property
    def health(self) -> PoolHealth:
        """The pool-wide counts from the last reconcile — the same numbers the
        degradation alarm uses, so log and admin UI cannot diverge."""
        return self._health

    @property
    def payable(self) -> frozenset[str]:
        """The refs this installation holds a key for as of the last rebuild, a
        caller's own included — what an admin read reports and the alarm counts."""
        return self._payable

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

    async def entries(self) -> tuple[list[LLMConfig], list[LLMConfig]]:
        """The stored pool and the models declared in code, apart. This read is not the
        alias clock — ``direct()`` comes through here on every call and must not pay a
        catalog parse."""
        configs = await self._registry.load()
        if self._overlay is None:
            return configs, []
        declared = list((await self._resolve_overlay()).configs)
        check_overlay(configs, declared)
        return configs, declared

    async def _resolve_overlay(self) -> DeclaredModels:
        """Resolve ``direct=`` once, however many callers arrive together: the callers
        that find the resolution dropped are requests, and unserialized each would
        parse — or where nothing is writable, fetch — the catalog for itself."""
        if self._declared is not None:
            return self._declared
        async with self._declared_lock:
            if self._declared is None and self._overlay is not None:
                declared = await self._overlay()
                # The same bootstrap `sync` gives the stored model_list, without which a
                # declared model is dead wherever secrets are not the environment.
                await self.seed_secrets(declared.configs)
                self._declared = declared
            return self._declared if self._declared is not None else DeclaredModels()

    async def rebuild(self, known: frozenset[str] | None = None) -> None:
        """Re-read the registry and re-derive everything learned: pool membership, the
        admin disabled map, and quality from the journal tail. ``known`` is the one
        listing of the secrets store the broker made just before, or ``None``."""
        # A caller's ref is the shared one behind its scope prefix; a ref itself never
        # carries a separator, so the last segment is the ref whatever the scope holds.
        self._scoped_refs = (
            frozenset(r.rsplit("/", 1)[-1] for r in known if "/" in r)
            if known is not None
            else frozenset()
        )
        stored, declared = await self.entries()
        self._empty = not stored and not declared
        await self._reconcile(stored, declared)
        await self._resync_disabled()
        if self._relearn is not None:
            await self._relearn()

    def check_not_empty(self) -> None:
        """Raise when the last rebuild found nothing at all. Only provisioning asks:
        a running pool that empties keeps serving nothing rather than failing a call."""
        if self._empty:
            raise EmptyRegistryError(_EMPTY_NOT_FILLED if self._autofill else _EMPTY_NO_AUTOFILL)

    async def _resync_disabled(self) -> None:
        if not isinstance(self._store, DisabledMapProtocol):
            return
        await self._store.seed_disabled(list(self._pool.configs))
        for name, flag in (await self._store.disabled_map()).items():
            if flag:
                self._pool.set_disabled(name)
            else:
                await self._pool.clear_disabled(name)

    async def _reconcile(self, stored: list[LLMConfig], declared: list[LLMConfig]) -> None:
        """Reconcile the pool with what the registry holds. A declared model is reached
        by name through ``broker.direct`` and never joins it."""
        names = {c.name for c in stored}
        for name in list(self._pool.configs):
            if name not in names:
                await self._pool.drop(name)
        for order, cfg in enumerate(stored):
            await self._pool.add(cfg, order=order)
        await self._measure(stored, declared)
        self._report_health()
        self._report_missing_keys()

    def _held(self, ref: str, shared: frozenset[str]) -> bool:
        """Whether this installation holds a key for ``ref`` at all — the shared value,
        or one belonging to a single caller. Which caller is not the measure's
        business; that a key is here, is."""
        return ref in shared or ref in self._scoped_refs

    async def _measure(self, managed: list[LLMConfig], direct: list[LLMConfig]) -> None:
        shared = await self._ring.payable(c.api_key_ref for c in managed)
        usable: set[str] = set()
        missing: dict[str, list[str]] = {}
        total: set[str] = set()
        for cfg in managed:
            if not cfg.api_key_ref:
                continue
            total.add(cfg.api_key_ref)
            if self._held(cfg.api_key_ref, shared):
                usable.add(cfg.api_key_ref)
            else:
                missing.setdefault(cfg.api_key_ref, []).append(cfg.name)
        self._payable = frozenset(usable)
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
            shared = await self._ring.resolve(cfg.api_key_ref) is not None
            if not self._held(cfg.api_key_ref, frozenset({cfg.api_key_ref} if shared else ())):
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
        """Mirror an already-merged model list into the registry and seed its keys.

        The merge decision — what the model list should be — belongs to
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

    async def present_refs(self, refs: Iterable[str]) -> frozenset[str]:
        """Which of ``refs`` a key resolves for here — what a sync report describes,
        never what it decides on."""
        return await self._ring.payable(refs)

    async def seed_secrets(self, configs: Sequence[LLMConfig]) -> None:
        """Copy any env-resolvable keys into a mutable secrets backend, preserving existing."""
        if not isinstance(self._secrets, MutableSecretsProtocol):
            return
        bootstrap = Secrets()
        for cfg in configs:
            if await resolve_ref(self._secrets, cfg.api_key_ref) is not None:
                continue  # already resolvable — preserve
            value = await resolve_ref(bootstrap, cfg.api_key_ref)
            if value is not None:
                await self._secrets.set(cfg.api_key_ref, value)

    def _require_mutable_registry(self) -> MutableRegistryProtocol:
        if not isinstance(self._registry, MutableRegistryProtocol):
            raise TypeError(
                f"{type(self._registry).__name__} does not support mutations"
                " (sync requires a mutable registry such as llmbroker.sqlite.Registry)",
            )
        return self._registry
