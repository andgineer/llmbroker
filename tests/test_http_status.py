"""Tests for the one HTTP-status vocabulary."""

import pytest

from llmbroker.http_status import (
    DETAIL_SNIPPET,
    ERROR_FLOOR,
    is_auth_failure,
    is_client_error,
    is_rate_limit,
    is_unavailable,
)

BOUNDARIES = (399, 400, 401, 403, 404, 418, 429, 499, 500, 503)


@pytest.mark.parametrize("code", BOUNDARIES)
def test_each_predicate_over_the_boundaries(code):
    assert is_rate_limit(code) is (code in (429, 503))
    assert is_unavailable(code) is (code == 503)
    assert is_auth_failure(code) is (code in (401, 403))
    assert is_client_error(code) is (code in (400, 404, 418, 499))


def test_every_4xx_is_exactly_one_of_the_three():
    """Invariant 10's subject: a rate limit and a rejected key are never the
    request's own fault, so `is_client_error` must exclude every code they claim."""
    for code in range(400, 500):
        claims = (is_rate_limit(code), is_auth_failure(code), is_client_error(code))
        assert sum(claims) == 1, code


def test_client_error_is_4xx_only():
    outside = [c for c in (*range(200, 400), *range(500, 600)) if is_client_error(c)]
    assert outside == []


def test_shared_limits():
    assert (ERROR_FLOOR, DETAIL_SNIPPET) == (400, 300)
