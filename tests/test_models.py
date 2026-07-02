"""Tests for onboarding DTOs and enums: EffortLevel, ValueLevel, KeyInfo."""

from llmbroker.models import EffortLevel, KeyInfo, ValueLevel


def test_effort_level_easiest_first_order():
    assert list(EffortLevel) == [
        EffortLevel.OAUTH,
        EffortLevel.SIGNUP,
        EffortLevel.VERIFY,
        EffortLevel.CONSOLE,
        EffortLevel.WAITLIST,
    ]
    assert list(EffortLevel).index(EffortLevel.OAUTH) < list(EffortLevel).index(EffortLevel.SIGNUP)


def test_value_level_most_desirable_first_order():
    assert list(ValueLevel) == [ValueLevel.HIGH, ValueLevel.GOOD, ValueLevel.NICHE]


def test_key_info_full():
    info = KeyInfo(
        api_key_ref="GROQ_API_KEY",
        effort=EffortLevel.SIGNUP,
        value=ValueLevel.GOOD,
        help="Create a free account.",
    )
    assert info.api_key_ref == "GROQ_API_KEY"
    assert info.effort is EffortLevel.SIGNUP
    assert info.value is ValueLevel.GOOD
    assert info.help == "Create a free account."


def test_key_info_partial_none_fields():
    info = KeyInfo(api_key_ref="K", effort=None, value=None, help="")
    assert info.effort is None
    assert info.value is None
