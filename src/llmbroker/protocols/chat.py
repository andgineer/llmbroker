"""Chat contract: what the tool loop drives — a routed caller and a direct client alike."""

from typing import Protocol, TypeVar


class ToolReply(Protocol):
    """One round's reply: the prose, and the tool calls it asks for."""

    @property
    def text(self) -> str: ...

    @property
    def tool_calls(self) -> list[dict] | None: ...


ReplyT_co = TypeVar("ReplyT_co", covariant=True)


class ChatProtocol(Protocol[ReplyT_co]):
    """A ``chat`` taking tools; an async one is the same port over an awaitable reply."""

    def chat(self, messages: list[dict], *, tools: list[dict] | None = None) -> ReplyT_co: ...
