"""State-store contract: persist LLM cooldown state, and share decayed learned
summaries, between requests and across cluster instances.

Optional, opt-in. Any stateless server (multiple workers, restarts, a load
balancer) needs it to keep cooldown state between requests, not only between
cluster nodes. The summary operations exist so concurrent instances can fold
in their own local evidence without a read-modify-write race — every backend
applies the delta as one server-side atomic operation.
"""

from typing import Protocol

from llmbroker.models import LLMState, QualitySummary


class StateStoreProtocol(Protocol):
    async def read(self, user_id: int | str | None = None) -> dict[str, LLMState]: ...
    async def write(self, name: str, state: LLMState, user_id: int | str | None = None) -> None: ...

    async def apply_summary_delta(  # noqa: PLR0913
        self,
        name: str,
        operation: str | None,
        kind: str,
        decay_pow: float,
        add_weight: float,
        add_good: float,
        add_weight_sq: float,
        add_count: int,
        user_id: int | str | None = None,
    ) -> None:
        """Atomically fold a batch of local events into the shared summary.

        Algebraically identical to applying ``add_count`` individual events one by
        one: ``weight <- weight * decay_pow + add_weight`` (likewise for
        ``weighted_good``; ``weight_sq`` uses ``decay_pow**2``; ``count`` is a plain
        add). Insert-if-absent: a summary with no prior row starts at exactly the
        delta values, since ``0 * decay_pow + add_weight == add_weight``.
        """
        ...

    async def read_summaries(
        self,
        user_id: int | str | None = None,
    ) -> dict[tuple[str, str | None, str], QualitySummary]: ...

    async def seed_summary(
        self,
        name: str,
        operation: str | None,
        kind: str,
        summary: QualitySummary,
        user_id: int | str | None = None,
    ) -> None:
        """Insert-if-absent — idempotent across racing instances warm-starting the same summary."""
        ...
