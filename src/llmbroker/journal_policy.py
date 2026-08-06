"""Journal policy shared by every store: the retention horizon, the purge
debounce, and the one shape of a quality record."""

import time
import uuid
from datetime import UTC, datetime, timedelta

from llmbroker.models import Call

RETENTION_DEFAULT = timedelta(days=90)
PURGE_INTERVAL = 3600.0


def quality_call(
    llm_name: str,
    operation: str | None,
    score: float,
    *,
    call_id: str | None,
    scope: str | None,
) -> Call:
    """Build the self-contained ``kind="quality"`` record — never joined to the call it rates."""
    return Call(
        id=str(uuid.uuid4()),
        llm_name=llm_name,
        operation=operation,
        trace_id=None,
        status=None,
        kind="quality",
        ts=datetime.now(UTC),
        quality_score=score,
        call_id=call_id,
        scope=scope,
    )


class PurgeClock:
    """Debounce for the retention purge: ``due()`` is true at most once per interval."""

    def __init__(self) -> None:
        self._last = float("-inf")

    def due(self) -> bool:
        now = time.monotonic()
        if now - self._last < PURGE_INTERVAL:
            return False
        self._last = now
        return True
