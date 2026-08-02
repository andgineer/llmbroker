"""Tests for onboarding DTOs: KeyInfo, SyncReport."""

from datetime import UTC, datetime

import llmbroker
import pytest

from llmbroker.models import KeyInfo, PendingKey, Retirement, SyncReport, key_hash


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


def _kept_line(report: SyncReport) -> str:
    return next(line for line in str(report).splitlines() if line.startswith("  kept:"))


def test_kept_sentence_names_the_provider_and_the_key_that_holds_the_entry():
    report = SyncReport(
        source="freetier",
        applied=True,
        kept=("openrouter-nemotron",),
        kept_refs=("OPENROUTER_API_KEY",),
    )
    assert _kept_line(report) == (
        "  kept: openrouter-nemotron — the lineup no longer carries OPENROUTER_API_KEY"
        " and this installation has a key for it, so it stays"
    )


def test_kept_sentence_for_several_entries_and_providers():
    report = SyncReport(
        source="freetier",
        applied=True,
        kept=("groq-a", "openr-b"),
        kept_refs=("GROQ_API_KEY", "OPENROUTER_API_KEY"),
    )
    assert _kept_line(report) == (
        "  kept: groq-a, openr-b — the lineup no longer carries GROQ_API_KEY,"
        " OPENROUTER_API_KEY and this installation has keys for them, so they stay"
    )


def test_kept_sentence_when_keys_are_per_user():
    """A scoped installation resolves no shared key by design, so absence proves
    nothing — and the report must say which of the two blind spots this is."""
    report = SyncReport(
        source="freetier",
        applied=True,
        kept=("groq-old",),
        kept_refs=("GROQ_API_KEY",),
        keys_visible=False,
        keys_scoped=True,
    )
    assert _kept_line(report) == (
        "  kept: groq-old — the lineup no longer carries GROQ_API_KEY, and keys are per"
        " user here so a missing one proves nothing — it stays"
    )


def test_kept_sentence_when_the_probe_resolved_nothing_at_all():
    report = SyncReport(
        source="freetier",
        applied=True,
        kept=("groq-old",),
        kept_refs=("GROQ_API_KEY",),
        keys_visible=False,
    )
    assert _kept_line(report) == (
        "  kept: groq-old — the lineup no longer carries GROQ_API_KEY, and no key resolved"
        " here at all so these are not the keys this lineup runs on — it stays"
    )


def test_a_retired_entry_shows_the_evidence_that_condemned_it():
    """A sync deleting an entry from the user's config has to justify itself in the
    one line an admin reads, or the verdict is uncheckable without the journal."""
    report = SyncReport(
        source="freetier",
        applied=True,
        removed=("groq-llama-3.3-70b",),
        retired=(
            Retirement(
                name="groq-llama-3.3-70b",
                http_status=401,
                since=datetime(2026, 7, 2, tzinfo=UTC),
            ),
        ),
    )
    line = next(line for line in str(report).splitlines() if line.startswith("  retired:"))
    assert line == (
        "  retired: groq-llama-3.3-70b — 401 since 2026-07-02, no successful call since;"
        " the lineup dropped it too"
    )


def test_a_retirement_without_a_timestamp_still_renders():
    """``Call.ts`` is optional, so a journal row that never carried one must not
    break the report the removal is announced in."""
    report = SyncReport(
        source="freetier",
        applied=True,
        retired=(Retirement(name="groq-old"),),
    )
    line = next(line for line in str(report).splitlines() if line.startswith("  retired:"))
    assert line == (
        "  retired: groq-old — a permanent failure, no successful call since;"
        " the lineup dropped it too"
    )


def test_an_orphaned_ref_is_reported_as_revocable():
    report = SyncReport(
        source="freetier",
        applied=True,
        removed=("groq-old",),
        orphan_refs=("GROQ_API_KEY",),
    )
    line = next(line for line in str(report).splitlines() if line.startswith("  unused key"))
    assert line == (
        "  unused key GROQ_API_KEY — nothing here uses it any more;"
        " revoke it at the provider if you do not need it"
    )


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
