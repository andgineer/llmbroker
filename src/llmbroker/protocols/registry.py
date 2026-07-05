"""Registry contract: load LLM configs; mutable backends also mirror a preset into them."""

from typing import Protocol, runtime_checkable

from llmbroker.models import KeyInfo, LLMConfig


class RegistryProtocol(Protocol):
    async def load(self, user_id: int | str | None = None) -> list[LLMConfig]: ...


@runtime_checkable
class MutableRegistryProtocol(RegistryProtocol, Protocol):
    async def mirror(self, configs: list[LLMConfig], user_id: int | str | None = None) -> None:
        """Total mirror: add entries absent from the store, update existing ones,
        delete stored entries absent from ``configs``. The only registry write path."""
        ...


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
