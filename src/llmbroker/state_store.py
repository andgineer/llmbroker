"""StateStore port protocol — persists LLM cooldown state between requests.

Optional, opt-in. Any stateless server (multiple workers, restarts, a load
balancer) needs it to keep cooldown state between requests, not only between
cluster nodes. Backends (redis/postgres/mongodb) land in P3; the protocol
is defined here.
"""

from typing import Protocol

from llmbroker.models import LLMState


class StateStoreProtocol(Protocol):
    async def read(self, user_id: int | str | None = None) -> dict[str, LLMState]: ...
    async def write(self, name: str, state: LLMState, user_id: int | str | None = None) -> None: ...
