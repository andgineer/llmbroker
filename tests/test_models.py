"""Tests for onboarding DTOs: KeyInfo."""

import math

import pytest

from llmbroker.models import KeyInfo, QualitySummary, key_hash


def test_key_info_full():
    info = KeyInfo(
        api_key_ref="GROQ_API_KEY",
        help="Create a free account.",
        extra={"effort": "signup", "value": "good"},
    )
    assert info.api_key_ref == "GROQ_API_KEY"
    assert info.help == "Create a free account."
    assert info.extra == {"effort": "signup", "value": "good"}


def test_key_info_no_extra():
    info = KeyInfo(api_key_ref="K", help="", extra={})
    assert info.extra == {}


# --- QualitySummary -----------------------------------------------------


def test_quality_summary_n_eff_is_one_after_exactly_one_event():
    s = QualitySummary()
    s.update(1.0, 0.9)
    assert s.n_eff == 1.0


def test_quality_summary_n_eff_saturates_to_variance_based_ess():
    """ESS ceiling is (1+d)/(1-d) — twice the weight ceiling 1/(1-d), not equal to it."""
    d = 9 / 11
    s = QualitySummary()
    for _ in range(500):
        s.update(1.0, d)
    assert math.isclose(s.n_eff, (1 + d) / (1 - d), rel_tol=1e-6)
    assert math.isclose(s.weight, 1 / (1 - d), rel_tol=1e-6)
    assert not math.isclose(s.n_eff, s.weight, rel_tol=1e-3)


def test_wilson_upper_hand_computed_zero_rate_single_event():
    """Closed form for p=0, n_eff=1: upper = z² / (1 + z²)."""
    s = QualitySummary(weight=1.0, weighted_good=0.0, weight_sq=1.0, count=1)
    z = 1.96
    expected = (z * z) / (1 + z * z)
    assert s.wilson_upper(z, min_count=1) == pytest.approx(expected, rel=1e-9)


def test_wilson_upper_full_rate_is_always_one():
    """Closed form for p=1 at any n: numerator and denominator are identical."""
    s = QualitySummary(weight=3.0, weighted_good=3.0, weight_sq=2.5, count=4)
    assert s.wilson_upper(1.96, min_count=1) == pytest.approx(1.0)


def test_wilson_upper_none_below_min_count_even_near_weight_ceiling():
    """The trust gate reads count, not weight — even weight near the 9/11-decay
    ceiling (5.5) must not bypass an insufficient count."""
    s = QualitySummary(weight=5.4, weighted_good=3.0, weight_sq=2.0, count=3)
    assert s.wilson_upper(1.96, min_count=10) is None


def test_wilson_upper_none_when_weight_is_zero():
    s = QualitySummary(weight=0.0, weighted_good=0.0, weight_sq=0.0, count=0)
    assert s.wilson_upper(1.96, min_count=0) is None


# --- key_hash -----------------------------------------------------------


def test_key_hash_is_deterministic_and_short():
    assert key_hash("sk-abc") == key_hash("sk-abc")
    assert len(key_hash("sk-abc")) == 12


def test_key_hash_differs_for_different_keys():
    assert key_hash("sk-abc") != key_hash("sk-xyz")
