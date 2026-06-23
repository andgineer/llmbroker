"""The live in-memory routing substrate behind ``AsyncBroker``.

One ``asyncio.Queue`` slot per LLM ⇒ at most one in-flight request per LLM.
A slot is withdrawn from the queue while the LLM is cooling after a 429/503 and
re-added when the cooldown expires. The pool also owns the in-memory
``LLMState`` (cooldown + quality counters) and persists it via the state store.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx

from llmbroker.broker.state import InMemoryState
from llmbroker.chat import retry_after_seconds
from llmbroker.models import LifecyclePhase, LLMConfig, LLMState
from llmbroker.protocols.state_store import StateStoreProtocol

logger = logging.getLogger("llmbroker.broker")

_DEFAULT_RATE_LIMIT_SEC = 60
_STORE_CACHE_TTL = 2.0  # seconds


class LLMPool:
    """The pool of LLM slots and their live cooldown / quality state."""

    def __init__(
        self,
        state_store: StateStoreProtocol | None,
        user_id: int | str | None,
    ) -> None:
        self._queue: asyncio.Queue[LLMConfig] = asyncio.Queue()
        self._configs: dict[str, LLMConfig] = {}
        self._resolved_keys: dict[str, str] = {}
        self._state = InMemoryState()
        self._state_store = state_store
        self._user_id = user_id
        self._store_cache: dict[str, LLMState] | None = None
        self._store_cache_expires: float = 0.0

    # ------------------------------------------------------------------
    # Membership / lookup
    # ------------------------------------------------------------------

    def __contains__(self, name: str) -> bool:
        return name in self._configs

    def __len__(self) -> int:
        return len(self._configs)

    @property
    def configs(self) -> dict[str, LLMConfig]:
        return self._configs

    def config(self, name: str) -> LLMConfig:
        return self._configs[name]

    def has_key(self, name: str) -> bool:
        return name in self._resolved_keys

    def resolved_key(self, name: str) -> str:
        return self._resolved_keys[name]

    # ------------------------------------------------------------------
    # Membership mutation
    # ------------------------------------------------------------------

    def add(self, cfg: LLMConfig, key: str | None) -> None:
        """Register/refresh a config. A ``None`` key leaves any prior key intact."""
        is_new = cfg.name not in self._configs
        self._configs[cfg.name] = cfg
        if key is not None:
            self._resolved_keys[cfg.name] = key
        if is_new:
            self._queue.put_nowait(cfg)

    def drop(self, name: str) -> None:
        self._configs.pop(name, None)
        self._resolved_keys.pop(name, None)

    # ------------------------------------------------------------------
    # Slot acquisition
    # ------------------------------------------------------------------

    async def acquire(self, wait: float | None) -> LLMConfig:
        if wait is None:
            return await self._queue.get()
        if wait == 0:
            return self._queue.get_nowait()
        return await asyncio.wait_for(self._queue.get(), timeout=wait)

    def release(self, config: LLMConfig) -> None:
        self._queue.put_nowait(config)

    # ------------------------------------------------------------------
    # Cooldown / state
    # ------------------------------------------------------------------

    def clear_cooling(self, name: str) -> None:
        self._state.clear_cooling(name)

    async def cool_down(self, config: LLMConfig, headers: httpx.Headers) -> None:
        """Withdraw the slot for the Retry-After window, persisting the new state."""
        delay = retry_after_seconds(headers, _DEFAULT_RATE_LIMIT_SEC)
        cooldown_until = datetime.now(UTC) + timedelta(seconds=delay)
        self._state.set_cooling(
            config.name,
            cooldown_until,
            self._state.fail_count(config.name) + 1,
        )
        if self._state_store is not None:
            await self._state_store.write(
                config.name,
                self._state.get_state(config.name),
                self._user_id,
            )
            self._store_cache = None
        loop = asyncio.get_running_loop()
        loop.call_later(float(delay), self._queue.put_nowait, config)
        logger.warning("LLM %s cooling for %ds", config.name, delay)

    def mark_quality_fail(self, name: str) -> None:
        self._state.record_quality_fail(name)

    def state(self, name: str) -> LLMState:
        return self._state.get_state(name)

    async def stored_states(self) -> dict[str, LLMState]:
        """Read persisted per-LLM state from the store, or ``{}`` if none configured."""
        if self._state_store is None:
            return {}
        return await self._state_store.read(self._user_id)

    async def _get_store_cache(self) -> dict[str, LLMState]:
        now = time.monotonic()
        if self._store_cache is not None and now < self._store_cache_expires:
            return self._store_cache
        self._store_cache = await self.stored_states()
        self._store_cache_expires = now + _STORE_CACHE_TTL
        return self._store_cache

    async def apply_shared_cooling(self, config: LLMConfig) -> bool:
        """Return True if shared store shows this LLM cooling and the slot was deferred.

        The caller must have already dequeued ``config`` via ``acquire()``; this method
        re-queues it via ``call_later`` and must not be called on a slot still in the queue.
        """
        if self._state_store is None:
            return False
        try:
            shared = await self._get_store_cache()
        except Exception:  # noqa: BLE001
            logger.warning(
                "LLM %s: shared-state read failed; proceeding without shared cooling",
                config.name,
            )
            return False
        stored = shared.get(config.name)
        if stored is None or stored.phase is not LifecyclePhase.COOLING:
            return False
        # A naive cooldown_until can't be compared to the aware now_utc below;
        # treat it as "not cooling" rather than risk a TypeError mid-routing.
        if stored.cooldown_until is None or stored.cooldown_until.tzinfo is None:
            return False
        now_utc = datetime.now(UTC)
        if stored.cooldown_until <= now_utc:
            return False
        local_fail_count = self._state.fail_count(config.name)
        self._state.set_cooling(
            config.name,
            stored.cooldown_until,
            max(local_fail_count, stored.fail_count),
        )
        delay = (stored.cooldown_until - now_utc).total_seconds()
        loop = asyncio.get_running_loop()
        loop.call_later(delay, self._queue.put_nowait, config)
        logger.info("LLM %s: shared cooldown applied, %.0fs remaining", config.name, delay)
        return True
