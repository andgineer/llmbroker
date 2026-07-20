"""Catalog: keep the live pool's membership in sync with the registry.

Loads configs from the registry, resolves their API keys via the secrets
backend, and reflects every change into the ``LLMPool``. The registry is a
pure mirror of a preset (see ``sync``) — nothing else writes it.
"""

import logging

from llmbroker.broker.pool import LLMPool
from llmbroker.models import LLMConfig
from llmbroker.protocols.registry import MutableRegistryProtocol, RegistryProtocol
from llmbroker.protocols.secrets import MutableSecretsProtocol, SecretsProtocol
from llmbroker.standalone.secrets import Secrets

logger = logging.getLogger("llmbroker.broker")


async def resolve_key(
    secrets: SecretsProtocol,
    api_key_ref: str,
    scope: str | None,
) -> str | None:
    """Resolve ``api_key_ref`` to a key: scope-prefixed (own) ref first, then shared."""
    if scope is not None:
        try:
            return await secrets.resolve(f"{scope}/{api_key_ref}")
        except KeyError:
            pass
    try:
        return await secrets.resolve(api_key_ref)
    except KeyError:
        return None


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
            raise RuntimeError(
                "registry is empty — call sync(preset) to mirror a preset into it before"
                " provisioning (e.g. `await broker.sync(preset)` or `python -m llmbroker sync`)",
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

    async def sync(self, preset: RegistryProtocol) -> None:
        """Mirror ``preset`` into the registry: add new entries, update existing
        ones, delete entries absent from the preset — the total mirror, nothing to
        preserve. Refuses (raises) a ``model`` identity change under an existing
        name, since that binds learned stats to the model name; a model bump must
        land under a new entry name instead.
        """
        registry = self._require_mutable_registry()
        source_configs = await preset.load()
        existing = {c.name: c for c in await registry.load()}
        for cfg in source_configs:
            current = existing.get(cfg.name)
            if current is not None and current.model != cfg.model:
                raise ValueError(
                    f"sync: refusing to change model for {cfg.name!r}"
                    f" (stored {current.model!r} vs preset {cfg.model!r}) — a model bump"
                    " must be a new entry name",
                )
        await registry.mirror(source_configs)
        await self._seed_secrets(source_configs)

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

    async def _seed_secrets(self, configs: list[LLMConfig]) -> None:
        """Copy any env-resolvable keys into a mutable secrets backend, preserving existing."""
        if not isinstance(self._secrets, MutableSecretsProtocol):
            return
        bootstrap = Secrets()
        for cfg in configs:
            try:
                await self._secrets.resolve(cfg.api_key_ref)
                continue  # already resolvable — preserve
            except KeyError:
                pass
            try:
                value = await bootstrap.resolve(cfg.api_key_ref)
            except KeyError:
                continue
            await self._secrets.set(cfg.api_key_ref, value)

    def _require_mutable_registry(self) -> MutableRegistryProtocol:
        if not isinstance(self._registry, MutableRegistryProtocol):
            raise TypeError(
                f"{type(self._registry).__name__} does not support mutations"
                " (sync requires a mutable registry such as llmbroker.sqlite.Registry)",
            )
        return self._registry
