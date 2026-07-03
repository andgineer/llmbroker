"""Registry contract: load LLM configs; mutable backends also CRUD them."""

from typing import Protocol, runtime_checkable

from llmbroker.models import KeyInfo, LLMConfig, LLMProfile


class RegistryProtocol(Protocol):
    async def load(self, user_id: int | str | None = None) -> list[LLMConfig]: ...

    async def read_profiles(self, user_id: int | str | None = None) -> dict[str, LLMProfile]: ...

    async def write_profile(
        self,
        name: str,
        profile: LLMProfile,
        user_id: int | str | None = None,
    ) -> None: ...


@runtime_checkable
class MutableRegistryProtocol(RegistryProtocol, Protocol):
    async def get(self, name: str, user_id: int | str | None = None) -> LLMConfig | None: ...
    async def add(self, cfg: LLMConfig, user_id: int | str | None = None) -> None: ...
    async def update(self, cfg: LLMConfig, user_id: int | str | None = None) -> None: ...
    async def remove(self, name: str, user_id: int | str | None = None) -> None: ...


@runtime_checkable
class KeyInfoProtocol(Protocol):
    """Optional capability: per-key onboarding metadata (effort, value, help).

    Maps each ``api_key_ref`` to a ``KeyInfo``. Keyed by the env-var name because
    one key is usually shared by several LLMs. A source without such metadata
    simply does not implement this protocol; callers probe with
    ``isinstance(reg, KeyInfoProtocol)``. It is independent of the broker — hosts
    query whichever registry they built.
    """

    async def key_info(self) -> dict[str, KeyInfo]: ...
