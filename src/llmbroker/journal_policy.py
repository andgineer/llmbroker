"""Journal policy shared by every store: the retention horizon, the purge
debounce, and the one shape of a quality record."""

import time
import uuid
from datetime import UTC, datetime, timedelta

RETENTION_DEFAULT = timedelta(days=90)
PURGE_INTERVAL = 3600.0

KIND_CALL = "call"
KIND_QUALITY = "quality"


def quality_row(call_id: str, score: float, scope: str | None) -> dict[str, object]:
    """The appended rating: names the call, carries no model and no operation."""
    return {
        "id": str(uuid.uuid4()),
        "kind": KIND_QUALITY,
        "call_id": call_id,
        "quality_score": score,
        "scope": scope,
        "called_at": datetime.now(UTC),
    }


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
