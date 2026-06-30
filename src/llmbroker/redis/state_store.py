"""Redis-backed state store over Redis hashes.

One hash per ``(user_id)`` scope.  Key format:
- ``llmbroker_state:__none__`` when ``user_id`` is ``None``
- ``llmbroker_state:{user_id}`` for any other value
"""

import json
from datetime import UTC, datetime
from typing import Self

import redis.asyncio as aioredis

from llmbroker.models import LifecyclePhase, LLMState, check_user_id

_TRUST_STORED_PHASES = frozenset({LifecyclePhase.OFFLINE, LifecyclePhase.PROBING})


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
        result: dict[str, LLMState] = {}
        now = datetime.now(UTC)
        for name, value in raw.items():
            data = json.loads(value)
            stored_phase = LifecyclePhase(data["phase"])
            cooldown_str = data.get("cooldown_until")
            cooldown_until = datetime.fromisoformat(cooldown_str) if cooldown_str else None
            fail_count = int(data["fail_count"])
            if stored_phase in _TRUST_STORED_PHASES:
                phase = stored_phase
                if cooldown_until is not None and cooldown_until <= now:
                    cooldown_until = None
            elif cooldown_until is not None and cooldown_until > now:
                phase = LifecyclePhase.COOLING
            elif stored_phase in {LifecyclePhase.AVAILABLE, LifecyclePhase.COOLING}:
                phase = LifecyclePhase.AVAILABLE
                cooldown_until = None
            else:
                raise ValueError(
                    f"Unexpected stored phase {stored_phase!r}: "
                    "add it to _TRUST_STORED_PHASES or handle it explicitly",
                )
            result[str(name)] = LLMState(
                phase=phase,
                cooldown_until=cooldown_until,
                fail_count=fail_count,
            )
        return result

    async def write(self, name: str, state: LLMState, user_id: int | str | None = None) -> None:
        check_user_id(user_id)
        if state.cooldown_until is not None and state.cooldown_until.tzinfo is None:
            raise ValueError("LLMState.cooldown_until must be timezone-aware")
        cooldown_iso = (
            state.cooldown_until.isoformat()
            if state.cooldown_until is not None and state.phase is not LifecyclePhase.AVAILABLE
            else None
        )
        payload: dict[str, object] = {
            "phase": state.phase.value,
            "fail_count": state.fail_count,
        }
        if cooldown_iso is not None:
            payload["cooldown_until"] = cooldown_iso
        await self._client.hset(self._scope_key(user_id), name, json.dumps(payload))

    async def aclose(self) -> None:
        return
