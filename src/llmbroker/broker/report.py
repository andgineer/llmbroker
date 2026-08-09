"""Turning what a sync decided into lines for a human: the CLI prints them, the
broker logs them."""

from collections.abc import Iterable

from llmbroker.broker.aliases import AliasChange, AliasFact
from llmbroker.models import SyncReport


def alias_lines(facts: Iterable[AliasFact]) -> tuple[str, ...]:
    """One line per fact — a re-spelled key ref is the one a reader must act on, so
    its line says what to set."""
    return tuple(
        f"{fact.alias}: api_key_ref {fact.was} -> {fact.now} — set {fact.now} before the next call"
        if fact.change is AliasChange.KEY_REF
        else f"{fact.alias}: {fact.was} -> {fact.now}"
        for fact in facts
    )


def _kept_line(report: SyncReport) -> str:
    subject, verb = ("it", "stays") if len(report.kept) == 1 else ("they", "stay")
    refs = ", ".join(report.kept_refs) or "their provider"
    head = f"the lineup no longer carries {refs}"
    if report.keys_visible:
        held = "a key for it" if len(report.kept_refs) == 1 else "keys for them"
        why = f"{head} and this installation has {held}, so {subject} {verb}"
    elif report.keys_scoped:
        why = (
            f"{head}, and keys are per user here so a missing one proves nothing — {subject} {verb}"
        )
    else:
        why = (
            f"{head}, and no key resolved here at all so these are not the keys this"
            f" lineup runs on — {subject} {verb}"
        )
    return f"  kept: {', '.join(report.kept)} — {why}"


def _retired_lines(report: SyncReport) -> list[str]:
    """One line each: a sync deleting an entry has to show its evidence, or an admin
    cannot check the verdict without going to the journal themselves."""
    lines = []
    for item in report.retired:
        status = item.http_status or "a permanent failure"
        since = f" since {item.since:%Y-%m-%d}" if item.since is not None else ""
        lines.append(
            f"  retired: {item.name} — {status}{since}, no successful call since;"
            " the lineup dropped it too",
        )
    return lines


def format_report(report: SyncReport) -> str:
    """The whole outcome as text, printed on every run including a no-op."""
    verb = "applied" if report.applied else "refused"
    lines = [
        f"sync {report.source}: {verb}"
        f" — {report.active_before} -> {report.active_after} entries with a key",
    ]
    for label, names in (
        ("added", report.added),
        ("updated", report.updated),
        ("removed", report.removed),
    ):
        if names:
            lines.append(f"  {label}: {', '.join(names)}")
    lines.extend(_retired_lines(report))
    if report.kept:
        lines.append(_kept_line(report))
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
