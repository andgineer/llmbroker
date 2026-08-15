"""Per-model aggregation of journal call records."""

from llmbroker.models import Call, CallStatus, LLMStats


def stats_from_calls(rows: list[Call]) -> dict[str, LLMStats]:
    """Aggregate call rows per model.

    ``rows`` are newest-first, as everywhere else in this codebase, so the first
    row seen for a model is its most recent and the last is its oldest.

    >>> from datetime import UTC, datetime
    >>> ts = datetime(2030, 1, 2, tzinfo=UTC)
    >>> rows = [
    ...     Call(id="2", llm_name="a", operation=None, trace_id=None,
    ...          status=CallStatus.OK, ts=ts),
    ...     Call(id="1", llm_name="a", operation=None, trace_id=None,
    ...          status=CallStatus.ERROR, ts=datetime(2030, 1, 1, tzinfo=UTC)),
    ... ]
    >>> stats = stats_from_calls(rows)["a"]
    >>> stats.total, stats.last_status
    (2, <CallStatus.OK: 'ok'>)
    >>> sorted((s.value, n) for s, n in stats.by_status.items())
    [('error', 1), ('ok', 1)]
    """
    totals: dict[str, int] = {}
    by_status: dict[str, dict[CallStatus, int]] = {}
    newest: dict[str, Call] = {}
    oldest: dict[str, Call] = {}
    for row in rows:
        name = row.llm_name
        totals[name] = totals.get(name, 0) + 1
        if row.status is not None:
            counts = by_status.setdefault(name, {})
            counts[row.status] = counts.get(row.status, 0) + 1
        newest.setdefault(name, row)
        oldest[name] = row
    return {
        name: LLMStats(
            total=total,
            by_status=by_status.get(name, {}),
            first_at=oldest[name].ts,
            last_at=newest[name].ts,
            last_status=newest[name].status,
        )
        for name, total in totals.items()
    }
