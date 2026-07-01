"""Redis-backed state store over Redis hashes.

One hash per ``(user_id)`` scope.  Key format:
- ``llmbroker_state:__none__`` when ``user_id`` is ``None``
- ``llmbroker_state:{user_id}`` for any other value
"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Self

import redis.asyncio as aioredis

from llmbroker.models import LifecyclePhase, LLMState, check_user_id, reconcile


class StateStore:
    """Redis-backed state store implementing ``StateStoreProtocol``.

    Accepts a pre-built async Redis client.  Use ``from_url`` to construct
    from a connection URL.
    """

    def __init__(self, client: aioredis.Redis) -> None:
        self._client = client

    @classmethod
    def from_url(cls, url: str, **kwargs: object) -> Self:
        return cls(aioredis.from_url(url, decode_responses=True, **kwargs))

    def _scope_key(self, user_id: int | str | None) -> str:
        if user_id is None:
            return "llmbroker_state:__none__"
        return f"llmbroker_state:{user_id}"

    async def read(self, user_id: int | str | None = None) -> dict[str, LLMState]:
        check_user_id(user_id)
        raw = await self._client.hgetall(self._scope_key(user_id))
        now = datetime.now(UTC)
        return {
            str(name): reconcile(LLMState.from_dict(json.loads(value)), now)
            for name, value in raw.items()
        }

    async def write(self, name: str, state: LLMState, user_id: int | str | None = None) -> None:
        check_user_id(user_id)
        if state.cooldown_until is not None and state.cooldown_until.tzinfo is None:
            raise ValueError("LLMState.cooldown_until must be timezone-aware")
        cooldown_until = (
            state.cooldown_until
            if state.cooldown_until is not None and state.phase is not LifecyclePhase.AVAILABLE
            else None
        )
        payload = replace(state, cooldown_until=cooldown_until).to_dict()
        await self._client.hset(self._scope_key(user_id), name, json.dumps(payload))

    async def aclose(self) -> None:
        return
