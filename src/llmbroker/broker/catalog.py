"""Catalog: keep the live pool's membership in sync with the registry.

Loads configs from the registry, resolves their API keys via the secrets
backend, seeds from a source registry when configured, and applies live
add/update/remove edits — reflecting every change into the ``LLMPool``.
"""

import logging

from llmbroker.broker.pool import LLMPool
from llmbroker.models import LLMConfig, SeedPolicy
from llmbroker.protocols.registry import MutableRegistryProtocol, RegistryProtocol
from llmbroker.protocols.secrets import MutableSecretsProtocol, SecretsProtocol
from llmbroker.standalone.secrets import Secrets

logger = logging.getLogger("llmbroker.broker")


class Catalog:
    """Reconciles the persistent registry into the live pool, and edits both."""

    def __init__(  # noqa: PLR0913
        self,
        registry: RegistryProtocol,
        secrets: SecretsProtocol,
        pool: LLMPool,
        *,
        seed: RegistryProtocol | None,
        seed_policy: SeedPolicy,
        user_id: int | str | None,
    ) -> None:
        self._registry = registry
        self._secrets = secrets
        self._pool = pool
        self._seed = seed
        self._seed_policy = seed_policy
        self._user_id = user_id

    async def provision(self) -> None:
        """Seed (if configured) then reconcile the pool with the registry.

        The caller serializes this one-time init; it is not re-entrant.
        """
        if self._seed is not None:
            await apply_seed(
                self._require_mutable_registry(),
                self._secrets,
                self._seed,
                self._seed_policy,
                self._user_id,
            )
        configs = await self._registry.load(user_id=self._user_id)
        names = {c.name for c in configs}
        for name in list(self._pool.configs):
            if name not in names:
                self._pool.drop(name)
        for cfg in configs:
            self._pool.add(cfg, await self._resolve_key(cfg))

    async def add(self, cfg: LLMConfig) -> None:
        registry = self._require_mutable_registry()
        if cfg.name in self._pool:
            raise ValueError(f"LLM {cfg.name!r} already exists; use update()")
        await registry.add(cfg, self._user_id)
        self._pool.add(cfg, await self._resolve_key(cfg))

    async def update(self, cfg: LLMConfig) -> None:
        registry = self._require_mutable_registry()
        if cfg.name not in self._pool:
            raise KeyError(cfg.name)
        await registry.update(cfg, self._user_id)
        self._pool.add(cfg, await self._resolve_key(cfg))

    async def remove(self, name: str) -> None:
        registry = self._require_mutable_registry()
        await registry.remove(name, self._user_id)
        self._pool.drop(name)

    async def _resolve_key(self, cfg: LLMConfig) -> str | None:
        try:
            return await self._secrets.resolve(cfg.api_key_ref, self._user_id)
        except KeyError:
            logger.info(
                "LLM %s: api_key_ref %r not resolved — inactive until the env var /"
                " secret is set; this is normal, the pool routes over whatever keys are present",
                cfg.name,
                cfg.api_key_ref,
            )
            return None

    def _require_mutable_registry(self) -> MutableRegistryProtocol:
        if not isinstance(self._registry, MutableRegistryProtocol):
            raise TypeError(
                f"{type(self._registry).__name__} does not support mutations"
                " (add/remove/seed require a mutable registry such as"
                " llmbroker.sqlite.Registry)",
            )
        return self._registry


async def apply_seed(
    registry: MutableRegistryProtocol,
    secrets: SecretsProtocol,
    source: RegistryProtocol,
    policy: SeedPolicy,
    user_id: int | str | None,
) -> None:
    """Mirror a source registry into the mutable store per ``policy``.

    Runs once at provision time, before the live pool exists; the caller holds
    the pool lock.
    """
    source_configs = await source.load()
    existing = {c.name: c for c in await registry.load(user_id=user_id)}

    if policy is SeedPolicy.IF_EMPTY and existing:
        return

    if policy is SeedPolicy.MIRROR:
        source_names = {c.name for c in source_configs}
        for name in list(existing):
            if name not in source_names:
                await registry.remove(name, user_id)
        for cfg in source_configs:
            if cfg.name in existing:
                await registry.update(cfg, user_id)
            else:
                await registry.add(cfg, user_id)
    else:  # ADD or IF_EMPTY (empty store)
        for cfg in source_configs:
            if cfg.name not in existing:
                await registry.add(cfg, user_id)

    await _seed_secrets(secrets, source_configs, user_id)


async def _seed_secrets(
    secrets: SecretsProtocol,
    configs: list[LLMConfig],
    user_id: int | str | None,
) -> None:
    """Copy any env-resolvable keys into a mutable secrets backend, preserving existing."""
    if not isinstance(secrets, MutableSecretsProtocol):
        return
    bootstrap = Secrets()
    for cfg in configs:
        try:
            await secrets.resolve(cfg.api_key_ref, user_id)
            continue  # already resolvable — preserve
        except KeyError:
            pass
        try:
            value = await bootstrap.resolve(cfg.api_key_ref)
        except KeyError:
            continue
        await secrets.set(cfg.api_key_ref, value, user_id)
