"""What a provider's HTTP status means, decided once: pure predicates over a status
code, with no package imports, so nothing compares numbers of its own."""

ERROR_FLOOR = 400
DETAIL_SNIPPET = 300

_TOO_MANY_REQUESTS = 429
_UNAVAILABLE = 503
_UNAUTHORIZED = 401
_FORBIDDEN = 403
_NOT_FOUND = 404
_SERVER_ERROR_FLOOR = 500


def is_rate_limit(code: int) -> bool:
    """Out of quota or the provider is down — cool the model down and fail over.

    >>> [is_rate_limit(c) for c in (200, 399, 429, 499, 500, 503)]
    [False, False, True, False, False, True]
    """
    return code in (_TOO_MANY_REQUESTS, _UNAVAILABLE)


def is_unavailable(code: int) -> bool:
    """Of the two rate-limit codes, the one that is the provider being down rather
    than this key's quota being spent — same cooldown, different journalled status.

    >>> [is_unavailable(c) for c in (429, 500, 502, 503)]
    [False, False, False, True]
    """
    return code == _UNAVAILABLE


def is_auth_failure(code: int) -> bool:
    """The key is rejected — drop the model and report the ref as dead.

    >>> [is_auth_failure(c) for c in (400, 401, 403, 404, 429, 500)]
    [False, True, True, False, False, False]
    """
    return code in (_UNAUTHORIZED, _FORBIDDEN)


def is_client_error(code: int) -> bool:
    """The request's own fault — fail over without cooling, and surface it once
    every candidate has said the same.

    >>> [is_client_error(c) for c in (399, 400, 401, 403, 404, 418, 429, 499, 500)]
    [False, True, False, False, True, True, False, True, False]
    """
    return ERROR_FLOOR <= code < _SERVER_ERROR_FLOOR and not (
        is_rate_limit(code) or is_auth_failure(code)
    )


def is_permanent(code: int) -> bool:
    """Retirement evidence — a failure no retry and no other caller can fix.

    >>> [is_permanent(c) for c in (400, 401, 403, 404, 429, 500, 503)]
    [False, True, True, True, False, False, False]
    """
    return is_auth_failure(code) or code == _NOT_FOUND
