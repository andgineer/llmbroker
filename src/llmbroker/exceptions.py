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


class UnknownModelError(LLMRequestError):
    """No registry entry matched the requested model name."""


class MissingKeyError(LLMRequestError):
    """The model's ``api_key_ref`` could not be resolved before the call.

    Distinct from ``AuthError``: nothing was sent to the provider — the key is
    simply not configured (set the env var or secrets backend). ``AuthError``
    means a key *was* sent and the provider rejected it.
    """


class LLMTimeoutError(LLMRequestError):
    """The request did not complete within its timeout."""


class ProviderError(LLMRequestError):
    """The provider returned an error response.

    ``status`` is the HTTP status code; ``detail`` is a short snippet of the
    response body, when available. Catch this to handle any provider failure
    coarsely, or one of its subclasses to react to a specific class of failure.
    """

    def __init__(self, message: str, *, status: int, detail: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


class AuthError(ProviderError):
    """The key was missing, malformed, or rejected (HTTP 401/403)."""


class RateLimitError(ProviderError):
    """The provider rate-limited or was temporarily unavailable (HTTP 429/503).

    ``retry_after`` is the server-advised wait in seconds, when the response
    carried a parseable ``Retry-After`` header.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int,
        detail: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message, status=status, detail=detail)
        self.retry_after = retry_after
