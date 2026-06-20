"""Private in-memory per-LLM live state.

Always-on internal detail, not a public port. Tracks cooldown_until and
fail_count per LLM name; ``phase`` is always DERIVED for AVAILABLE/COOLING
(never stored).
"""

from datetime import UTC, datetime

from llmbroker.models import LifecyclePhase, LLMState


class InMemoryState:
    """Per-LLM cooldown/fail tracking, keyed by LLM name."""

    def __init__(self) -> None:
        self._cooldown: dict[str, datetime] = {}
        self._fail_count: dict[str, int] = {}

    def get_state(self, name: str) -> LLMState:
        cooldown_until = self._cooldown.get(name)
        fail_count = self._fail_count.get(name, 0)
        now = datetime.now(UTC)
        if cooldown_until is not None and cooldown_until > now:
            phase = LifecyclePhase.COOLING
        else:
            phase = LifecyclePhase.AVAILABLE
            cooldown_until = None
        return LLMState(phase=phase, cooldown_until=cooldown_until, fail_count=fail_count)

    def set_cooling(self, name: str, cooldown_until: datetime, fail_count: int) -> None:
        self._cooldown[name] = cooldown_until
        self._fail_count[name] = fail_count

    def clear_cooling(self, name: str) -> None:
        self._cooldown.pop(name, None)

    def fail_count(self, name: str) -> int:
        return self._fail_count.get(name, 0)

    def record_quality_fail(self, name: str) -> None:
        self._fail_count[name] = self._fail_count.get(name, 0) + 1
