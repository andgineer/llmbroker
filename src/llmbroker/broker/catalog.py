"""Catalog: keep the live pool's membership in sync with the registry.

Loads configs from the registry, resolves their API keys via the secrets
backend, and reflects every change into the ``LLMPool``. ``apply`` is the only
registry write path; what it writes is decided in ``broker.upstream``.
"""

import logging

from llmbroker.broker.pool import LLMPool
from llmbroker.exceptions import EmptyRegistryError
from llmbroker.models import LLMConfig
from llmbroker.protocols.registry import MutableRegistryProtocol, RegistryProtocol
from llmbroker.protocols.secrets import MutableSecretsProtocol, SecretsProtocol
from llmbroker.standalone.secrets import Secrets

logger = logging.getLogger("llmbroker.broker")


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


class Catalog:
    """Reconciles the persistent registry into the live pool, and mirrors presets into it."""

    def __init__(
        self,
        registry: RegistryProtocol,
        secrets: SecretsProtocol,
        pool: LLMPool,
        *,
        scope: str | None,
    ) -> None:
        self._registry = registry
        self._secrets = secrets
        self._pool = pool
        self._scope = scope

    async def provision(self) -> None:
        """Reconcile the pool with the registry. The caller serializes this
        one-time init; it is not re-entrant. Raises if the registry is empty."""
        configs = await self._registry.load()
        if not configs:
            raise EmptyRegistryError(
                "registry is empty — sync a lineup into it before provisioning, e.g."
                ' `await broker.sync("freetier")` from your own entrypoint, or open the'
                ' broker with AsyncBroker(..., sync="freetier")',
            )
        await self._reconcile(configs)

    async def resync(self) -> None:
        """Re-read the registry and reconcile pool membership — no emptiness check.

        Called by the debounced journal rebuild so registry edits and key changes
        from other processes/nodes take effect on a running broker.
        """
        configs = await self._registry.load()
        await self._reconcile(configs)

    async def _reconcile(self, configs: list[LLMConfig]) -> None:
        """Reconcile only the pooled entries into the live pool. ``pooled=False``
        entries stay in the registry (reachable via ``broker.direct``) but never
        join the routed pool."""
        pooled = [c for c in configs if c.pooled]
        names = {c.name for c in pooled}
        for name in list(self._pool.configs):
            if name not in names:
                await self._pool.drop(name)
        for order, cfg in enumerate(pooled):
            await self._pool.add(cfg, await self._resolve_key(cfg), order=order)

    async def apply(self, configs: list[LLMConfig]) -> None:
        """Mirror an already-merged lineup into the registry and seed its keys.

        The merge decision — what the lineup should be — belongs to
        ``broker.upstream``; this half only writes it.
        """
        registry = self._require_mutable_registry()
        await registry.mirror(configs)
        await self.seed_secrets(configs)

    async def _resolve_key(self, cfg: LLMConfig) -> str | None:
        key = await resolve_key(self._secrets, cfg.api_key_ref, self._scope)
        if key is None:
            logger.info(
                "LLM %s: api_key_ref %r not resolved — inactive until the env var /"
                " secret is set; this is normal, the pool routes over whatever keys are present",
                cfg.name,
                cfg.api_key_ref,
            )
        return key

    async def seed_secrets(self, configs: list[LLMConfig]) -> None:
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
