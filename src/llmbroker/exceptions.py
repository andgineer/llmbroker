"""All exceptions a caller of llmbroker may catch, in one place."""

from datetime import datetime


class LLMRequestError(Exception):
    """Base: this request could not be completed."""


class NoLLMAvailableError(LLMRequestError):
    """No LLM slot was available for this request.

    ``reason`` is one of:

    - ``"empty_pool"`` — the pool has zero slots.
    - ``"no_keys"`` — slots exist but none has a resolved key.
    - ``"all_disabled"`` — keyed slots exist but every one is admin-disabled.
    - ``"excluded"`` — every candidate was excluded for this request (internal).
    - ``"timeout"`` — the deadline expired (or nothing is free right now);
      ``retry_at`` carries the earliest known return time, when known.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        retry_at: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.retry_at = retry_at
