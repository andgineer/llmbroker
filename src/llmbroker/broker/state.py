"""Private in-memory per-LLM live state.

Always-on internal detail of the broker, not a public backend. Tracks
cooldown_until and fail_count per LLM name; ``phase`` is always DERIVED for
AVAILABLE/COOLING (never stored).
"""

from datetime import UTC, datetime

from llmbroker.models import LifecyclePhase, LLMState


class InMemoryState:
    """Per-LLM cooldown/fail tracking, keyed by LLM name."""

    def __init__(self) -> None:
        self._cooldown: dict[str, datetime] = {}
        self._fail_count: dict[str, int] = {}
        self._phase_override: dict[str, LifecyclePhase] = {}

    def get_state(self, name: str) -> LLMState:
        override = self._phase_override.get(name)
        if override in (LifecyclePhase.OFFLINE, LifecyclePhase.PROBING):
            return LLMState(
                phase=override,
                cooldown_until=self._cooldown.get(name),
                fail_count=self._fail_count.get(name, 0),
            )
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

    def set_phase_override(self, name: str, phase: LifecyclePhase) -> None:
        self._phase_override[name] = phase

    def clear_phase_override(self, name: str) -> None:
        self._phase_override.pop(name, None)

    def clear_cooling(self, name: str) -> None:
        self._cooldown.pop(name, None)

    def fail_count(self, name: str) -> int:
        return self._fail_count.get(name, 0)

    def record_quality_fail(self, name: str) -> None:
        self._fail_count[name] = self._fail_count.get(name, 0) + 1
