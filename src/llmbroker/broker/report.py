"""Turning what a sync decided into lines for a human: the CLI prints them, the
broker logs them."""

from collections.abc import Iterable

from llmbroker.broker.aliases import AliasChange, AliasFact
from llmbroker.models import SyncReport


def alias_lines(facts: Iterable[AliasFact]) -> tuple[str, ...]:
    """One line per fact — a re-spelled key ref is the one a reader must act on, so
    its line says what to set."""
    return tuple(_alias_line(fact) for fact in facts)


def _alias_line(fact: AliasFact) -> str:
    if fact.change is AliasChange.KEY_REF:
        return (
            f"{fact.alias}: api_key_ref {fact.was} -> {fact.now}"
            f" — set {fact.now} before the next call"
        )
    if fact.change is AliasChange.DROPPED:
        return (
            f"{fact.alias}: the paid catalog no longer carries it"
            f" — answering from {fact.was}, the resolution already in use"
        )
    return f"{fact.alias}: {fact.was} -> {fact.now}"


def format_report(report: SyncReport) -> str:
    """The whole outcome as text, printed on every run including a no-op."""
    verb = "applied" if report.applied else "refused"
    lines = [
        (
            f"sync {report.source}: {verb}"
            f" — {report.active_before} -> {report.active_after} entries with a key"
        ),
    ]
    for label, names in (
        ("added", report.added),
        ("updated", report.updated),
        ("removed", report.removed),
    ):
        if names:
            lines.append(f"  {label}: {', '.join(names)}")
    for ref in report.orphan_refs:
        lines.append(
            f"  unused key {ref} — nothing here uses it any more;"
            " revoke it at the provider if you do not need it",
        )
    for pending in report.pending_keys:
        lines.append(
            f"  pending key {pending.api_key_ref} — holds back {', '.join(pending.entry_names)}",
        )
        lines.extend(f"      {line}" for line in pending.help.splitlines() if line.strip())
    if len(lines) == 1:
        lines.append("  no changes")
    return "\n".join(lines)
