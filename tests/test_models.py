"""Tests for onboarding DTOs: KeyInfo."""

from llmbroker.models import KeyInfo, key_hash


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


# --- key_hash -----------------------------------------------------------


def test_key_hash_is_deterministic_and_short():
    assert key_hash("sk-abc") == key_hash("sk-abc")
    assert len(key_hash("sk-abc")) == 12


def test_key_hash_differs_for_different_keys():
    assert key_hash("sk-abc") != key_hash("sk-xyz")
