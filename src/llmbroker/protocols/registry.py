"""Registry contract: load LLM configs; mutable backends also mirror a preset into them."""

from typing import Protocol, runtime_checkable

from llmbroker.models import KeyInfo, LLMConfig


class RegistryProtocol(Protocol):
    async def load(self) -> list[LLMConfig]: ...


@runtime_checkable
class MutableRegistryProtocol(RegistryProtocol, Protocol):
    async def mirror(self, configs: list[LLMConfig]) -> None:
        """Total mirror: add entries absent from the store, update existing ones,
        delete stored entries absent from ``configs``. The only registry write path."""
        ...


@runtime_checkable
class KeyInfoProtocol(Protocol):
    """Optional capability: per-key onboarding metadata, keyed by ``api_key_ref``
    because one key is usually shared by several LLMs. A source without it simply
    does not implement this protocol, and callers probe with ``isinstance``."""

    async def key_info(self) -> dict[str, KeyInfo]: ...
