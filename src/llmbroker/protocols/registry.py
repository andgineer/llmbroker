"""Registry contract: load LLM configs; mutable backends also CRUD them."""

from typing import Protocol, runtime_checkable

from llmbroker.models import LLMConfig


class RegistryProtocol(Protocol):
    async def load(self, user_id: int | str | None = None) -> list[LLMConfig]: ...


@runtime_checkable
class MutableRegistryProtocol(RegistryProtocol, Protocol):
    async def get(self, name: str, user_id: int | str | None = None) -> LLMConfig | None: ...
    async def add(self, cfg: LLMConfig, user_id: int | str | None = None) -> None: ...
    async def update(self, cfg: LLMConfig, user_id: int | str | None = None) -> None: ...
    async def remove(self, name: str, user_id: int | str | None = None) -> None: ...
