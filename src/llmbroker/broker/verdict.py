"""Failure classifications shared by atomic and streaming routed attempts."""

from dataclasses import dataclass

import httpx

from llmbroker.chat import provider_error, retry_after_seconds
from llmbroker.exceptions import InvalidProviderResponseError, ProviderError
from llmbroker.http_status import (
    DETAIL_SNIPPET,
    is_auth_failure,
    is_client_error,
    is_rate_limit,
    is_unavailable,
)
from llmbroker.models import CallStatus

DEFAULT_RATE_LIMIT_SEC = 60
FAILOVER_ERRORS = (
    httpx.HTTPStatusError,
    httpx.TransportError,
    InvalidProviderResponseError,
    OSError,
)


@dataclass(frozen=True)
class Failed:
    """One candidate is done for this request.

    ``error`` is set only for a genuine client error, so the router can decide
    whether to surface it after every candidate is exhausted.
    """

    error: ProviderError | None


@dataclass(frozen=True)
class BudgetExpired:
    """The caller's own wait budget ran out during an attempt."""


@dataclass(frozen=True)
class Verdict:
    """How one failed attempt is disposed of and journaled."""

    status: CallStatus
    detail: str | None
    http_status: int | None = None
    cool_base: float | None = None
    outcome: Failed | BudgetExpired | None = None


def classify_status(exc: httpx.HTTPStatusError) -> Verdict:
    code = exc.response.status_code
    detail = exc.response.text[:DETAIL_SNIPPET]
    if is_rate_limit(code):
        status = CallStatus.UNAVAILABLE if is_unavailable(code) else CallStatus.RATE_LIMITED
        base = retry_after_seconds(exc.response.headers, DEFAULT_RATE_LIMIT_SEC)
        return Verdict(status, detail, http_status=code, cool_base=base)
    if is_auth_failure(code):
        return Verdict(CallStatus.ERROR, detail, http_status=code, outcome=Failed(error=None))
    if is_client_error(code):
        return Verdict(
            CallStatus.ERROR,
            detail,
            http_status=code,
            outcome=Failed(error=provider_error(code, detail, exc.response.headers)),
        )
    return Verdict(CallStatus.ERROR, detail, http_status=code, cool_base=DEFAULT_RATE_LIMIT_SEC)


def classify(exc: Exception, *, budget_bound: bool) -> Verdict:
    """Map a failed attempt onto cooldown, retry, or caller-budget expiry."""
    if isinstance(exc, httpx.HTTPStatusError):
        return classify_status(exc)
    if isinstance(exc, InvalidProviderResponseError):
        return Verdict(CallStatus.ERROR, exc.detail, cool_base=DEFAULT_RATE_LIMIT_SEC)
    if budget_bound and isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return Verdict(
            CallStatus.ERROR,
            f"wait budget exhausted: {type(exc).__name__}",
            cool_base=DEFAULT_RATE_LIMIT_SEC,
            outcome=BudgetExpired(),
        )
    return Verdict(CallStatus.ERROR, type(exc).__name__, cool_base=DEFAULT_RATE_LIMIT_SEC)
