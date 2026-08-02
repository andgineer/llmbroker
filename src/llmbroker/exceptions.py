"""All exceptions a caller of llmbroker may catch, in one place."""

from datetime import datetime

from llmbroker.models import SyncReport


class LLMBrokerError(RuntimeError):
    """Base: a lifecycle failure — provisioning or storage, not one request.

    Subclasses ``RuntimeError`` so a host that already catches ``RuntimeError``
    around provisioning keeps working.
    """


class EmptyRegistryError(LLMBrokerError):
    """The registry holds no configs — nothing has been synced into it yet.

    The request-time sibling is ``NoLLMAvailableError(reason="empty_pool")``:
    this one says nothing is configured, that one says nothing is usable now.
    """


class SyncRefusedError(LLMBrokerError):
    """A sync result was not applied — it would have emptied a working registry.

    ``report`` carries what the merge would have done, so a caller can log or
    forward the facts that led to the refusal.
    """

    def __init__(self, message: str, *, report: SyncReport) -> None:
        super().__init__(message)
        self.report = report


class SchemaVersionError(LLMBrokerError):
    """The store holds a schema version this release cannot use."""

    def __init__(self, message: str, *, found: int, expected: int) -> None:
        super().__init__(message)
        self.found = found
        self.expected = expected


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


class PoolModelError(LLMRequestError):
    """``direct()`` was pointed at a preset-managed pool entry.

    Pool models are anonymous: reach them through ``ask``/``chat``/``stream``,
    which route and learn. A model you want to name is a ``[[custom]]`` entry.
    """


class MissingKeyError(LLMRequestError):
    """The model's ``api_key_ref`` could not be resolved before the call.

    Distinct from ``AuthError``: nothing was sent to the provider — the key is
    simply not configured (set the env var or secrets backend). ``AuthError``
    means a key *was* sent and the provider rejected it.
    """


class LLMTimeoutError(LLMRequestError):
    """The request did not complete within its timeout."""


class ToolLoopLimitError(LLMRequestError):
    """The tool loop hit ``max_steps`` without a tool-call-free reply.

    Raised rather than returning an empty string: the error contract admits a
    result or an exception, never silence. A caller that prefers the partial
    conversation catches this.
    """


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


class InvalidProviderResponseError(LLMRequestError):
    """The provider answered 200 with a body that is not a chat completion.

    A 200 carrying garbage is a provider-side failure like a 5xx: the router
    cools the model down and fails over. ``model`` names it; ``detail`` is a
    truncated snippet of the body.
    """

    def __init__(self, message: str, *, model: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.model = model
        self.detail = detail


class StreamInterruptedError(LLMRequestError):
    """A pooled stream died after it had already emitted deltas.

    Failover is impossible once output has reached the caller, so the broker
    raises instead of retrying elsewhere: the deltas already yielded stand, and
    the rest of the answer is lost. ``llm_name`` names the model; ``__cause__``
    carries the underlying provider or transport error.
    """

    def __init__(self, message: str, *, llm_name: str) -> None:
        super().__init__(message)
        self.llm_name = llm_name


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
