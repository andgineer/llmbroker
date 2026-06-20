"""SharedState port protocol — the cluster coordination seam.

Optional, opt-in, cluster-only. Backends (redis/postgres/mongodb) land in P3;
P1 defines only the protocol.
"""

from typing import Protocol

from llmbroker.models import LLMState


class SharedStateProtocol(Protocol):
    async def read(self) -> dict[str, LLMState]: ...
    async def write(self, name: str, state: LLMState) -> None: ...
