"""The lines a sync report is printed and logged as, and the alias facts beside it."""

import llmbroker
from llmbroker.broker.aliases import AliasChange, AliasFact
from llmbroker.broker.report import alias_lines, format_report
from llmbroker.models import PendingKey, SyncReport


def _pending(ref, names, help_text=""):
    return PendingKey(api_key_ref=ref, help=help_text, entry_names=tuple(names))


def test_the_renderer_is_public_surface():
    """``last_sync_report`` is public and a host forwards it to an admin channel, so
    the text it is documented to print has to be reachable without a private import."""
    assert llmbroker.format_report is format_report
    assert "format_report" in llmbroker.__all__


def test_no_op_report_says_so():
    text = format_report(
        SyncReport(source="freetier", applied=True, active_before=3, active_after=3)
    )
    assert text.splitlines() == [
        "sync freetier: applied — 3 -> 3 entries with a key",
        "  no changes",
    ]


def test_report_lists_each_non_empty_section_once():
    text = format_report(
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
    assert format_report(SyncReport(source="freetier", applied=False)).startswith(
        "sync freetier: refused",
    )


def test_an_orphaned_ref_is_reported_as_revocable():
    report = SyncReport(
        source="freetier",
        applied=True,
        removed=("groq-old",),
        orphan_refs=("GROQ_API_KEY",),
    )
    line = next(
        line for line in format_report(report).splitlines() if line.startswith("  unused key")
    )
    assert line == (
        "  unused key GROQ_API_KEY — nothing here uses it any more;"
        " revoke it at the provider if you do not need it"
    )


def test_pending_key_renders_its_help():
    text = format_report(
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
    text = format_report(
        SyncReport(
            source="freetier",
            applied=True,
            added=("gem",),
            pending_keys=(_pending("GEMINI_API_KEY", ["gem"]),),
        ),
    )
    assert text.splitlines()[-1] == "  pending key GEMINI_API_KEY — holds back gem"


# --- alias facts ---------------------------------------------------------


def test_a_moved_alias_renders_as_the_version_step():
    lines = alias_lines(
        [
            AliasFact(
                change=AliasChange.MODEL, alias="opus", was="claude-opus-4-8", now="claude-opus-5"
            )
        ],
    )
    assert lines == ("opus: claude-opus-4-8 -> claude-opus-5",)


def test_a_moved_key_ref_says_what_to_set():
    """The one change that needs the user to do something, and it can arrive with no
    model change at all."""
    lines = alias_lines(
        [
            AliasFact(
                change=AliasChange.KEY_REF, alias="opus", was="ANTHROPIC_KEY", now="CLAUDE_KEY"
            )
        ],
    )
    assert lines == (
        "opus: api_key_ref ANTHROPIC_KEY -> CLAUDE_KEY — set CLAUDE_KEY before the next call",
    )
