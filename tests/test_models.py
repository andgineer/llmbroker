"""Tests for onboarding DTOs: KeyInfo, SyncReport."""

import llmbroker
import pytest

from llmbroker.models import KeyInfo, PendingKey, SyncReport, key_hash


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


# --- SyncReport ---------------------------------------------------------


def _pending(ref, names, help_text=""):
    return PendingKey(api_key_ref=ref, help=help_text, entry_names=tuple(names))


def test_sync_report_defaults_to_a_no_op():
    report = SyncReport(source="freetier", applied=True)
    assert report.added == report.updated == report.removed == report.kept == ()
    assert report.pending_keys == ()


def test_no_op_report_says_so():
    text = str(SyncReport(source="freetier", applied=True, active_before=3, active_after=3))
    assert text.splitlines() == [
        "sync freetier: applied — 3 -> 3 entries with a key",
        "  no changes",
    ]


def test_report_lists_each_non_empty_section_once():
    text = str(
        SyncReport(
            source="llms.toml",
            applied=True,
            added=("a", "b"),
            updated=("c",),
            removed=("d",),
            active_before=2,
            active_after=3,
        ),
    )
    assert text.splitlines() == [
        "sync llms.toml: applied — 2 -> 3 entries with a key",
        "  added: a, b",
        "  updated: c",
        "  removed: d",
    ]
    assert "kept" not in text
    assert "no changes" not in text


def test_refused_report_says_refused():
    assert str(SyncReport(source="freetier", applied=False)).startswith(
        "sync freetier: refused",
    )


def test_kept_sentence_with_one_unlocking_ref():
    report = SyncReport(
        source="freetier",
        applied=True,
        added=("gemini-2.0-flash",),
        kept=("groq-llama-3.3-70b",),
        pending_keys=(_pending("GEMINI_API_KEY", ["gemini-2.0-flash"]),),
    )
    assert report.unlocking_refs() == ("GEMINI_API_KEY",)
    kept_line = next(line for line in str(report).splitlines() if line.startswith("  kept:"))
    assert kept_line == (
        "  kept: groq-llama-3.3-70b — upstream dropped it and no replacement is usable;"
        " set GEMINI_API_KEY and the next sync removes it"
    )


def test_kept_sentence_with_several_unlocking_refs():
    report = SyncReport(
        source="freetier",
        applied=True,
        added=("gem", "openr"),
        kept=("groq-a", "groq-b"),
        pending_keys=(
            _pending("GEMINI_API_KEY", ["gem"]),
            _pending("OPENROUTER_API_KEY", ["openr"]),
        ),
    )
    assert report.unlocking_refs() == ("GEMINI_API_KEY", "OPENROUTER_API_KEY")
    kept_line = next(line for line in str(report).splitlines() if line.startswith("  kept:"))
    assert kept_line == (
        "  kept: groq-a, groq-b — upstream dropped them and no replacement is usable;"
        " set any of GEMINI_API_KEY, OPENROUTER_API_KEY and the next sync removes them"
    )


def test_kept_sentence_without_any_unlocking_ref():
    """A provider left and nothing arrived: no key would change the outcome, so the
    sentence must not offer one."""
    report = SyncReport(source="freetier", applied=True, kept=("groq-llama-3.3-70b",))
    assert report.unlocking_refs() == ()
    kept_line = next(line for line in str(report).splitlines() if line.startswith("  kept:"))
    assert kept_line == (
        "  kept: groq-llama-3.3-70b — upstream dropped it and nothing arrived to replace it"
    )


def test_a_kept_entrys_own_missing_key_does_not_unlock_it():
    """Only an arrival's key pays for a removal — the dropped entry's own ref
    resolving would change nothing."""
    report = SyncReport(
        source="freetier",
        applied=True,
        kept=("groq-old",),
        pending_keys=(_pending("GROQ_API_KEY", ["groq-old"]),),
    )
    assert report.unlocking_refs() == ()


def test_pending_key_renders_its_help():
    text = str(
        SyncReport(
            source="freetier",
            applied=True,
            added=("gem",),
            pending_keys=(
                _pending("GEMINI_API_KEY", ["gem"], "Sign up at example.com\nfree tier"),
            ),
        ),
    )
    assert "  pending key GEMINI_API_KEY — holds back gem" in text
    assert "      Sign up at example.com" in text
    assert "      free tier" in text


def test_pending_key_without_help_renders_one_line():
    text = str(
        SyncReport(
            source="freetier",
            applied=True,
            added=("gem",),
            pending_keys=(_pending("GEMINI_API_KEY", ["gem"]),),
        ),
    )
    assert text.splitlines()[-1] == "  pending key GEMINI_API_KEY — holds back gem"


def test_report_types_and_refusal_are_top_level():
    assert llmbroker.SyncReport is SyncReport
    assert llmbroker.PendingKey is PendingKey
    exc = llmbroker.SyncRefusedError("nope", report=SyncReport(source="s", applied=False))
    assert isinstance(exc, llmbroker.LLMBrokerError)
    assert exc.report.source == "s"
    with pytest.raises(llmbroker.SyncRefusedError):
        raise exc
