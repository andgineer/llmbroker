"""The live in-memory routing substrate behind ``AsyncBroker``.

One ``asyncio.Queue`` slot per LLM ⇒ at most one in-flight request per LLM.
A slot is withdrawn from the queue while the LLM is cooling after a 429/503 and
re-added when the cooldown expires. The pool also owns the in-memory
``LLMState`` (cooldown + quality counters) and persists it via the state store.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx

from llmbroker.broker.state import InMemoryState
from llmbroker.chat import retry_after_seconds
from llmbroker.models import LLMConfig, LLMState
from llmbroker.protocols.state_store import StateStoreProtocol

logger = logging.getLogger("llmbroker.broker")

_DEFAULT_RATE_LIMIT_SEC = 60


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
