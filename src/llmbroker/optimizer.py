"""The ``Optimizer`` knob — per-LLM failure bookkeeping, and per-(model, operation)
quality-window demotion verdicts feeding the pool's demoted-last selection order."""

import logging
from collections import deque
from dataclasses import dataclass, field
from statistics import NormalDist

logger = logging.getLogger("llmbroker.broker")


def _z_score(confidence: float) -> float:
    """Two-sided z for a given confidence level, e.g. 0.95 -> ~1.96."""
    return NormalDist().inv_cdf((1 + confidence) / 2)


def wilson_upper(scores: list[float], z: float) -> float:
    """Wilson-score upper bound of the mean of ``scores`` at confidence ``z``.

    >>> round(wilson_upper([1.0] * 25 + [0.0] * 5, 1.96), 4)
    0.9266
    """
    n = len(scores)
    p = sum(scores) / n
    z2 = z * z
    center = p + z2 / (2 * n)
    margin = z * ((p * (1 - p) / n) + z2 / (4 * n * n)) ** 0.5
    denom = 1 + z2 / n
    return (center + margin) / denom


@dataclass
class Optimizer:
    """Consecutive-failure counter for backoff, plus per-(model, operation) sliding
    windows of raw quality ratings backing the demoted-last selection order."""

    max_delay: float = 3600.0
    backoff_factor: float = 2.0
    quality_floor: float = 0.3
    quality_confidence: float = 0.95  # z for the Wilson upper bound
    quality_window: int = 30  # ratings kept per (model, operation)
    quality_min_count: int = 10  # verdicts need at least this many

    _rl_fail_count: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _scores: dict[tuple[str, str | None], deque] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def rl_fail_count(self, llm_name: str) -> int:
        return self._rl_fail_count.get(llm_name, 0)

    def on_rate_limited(self, llm_name: str) -> None:
        """Increment the consecutive-failure count the router reads for its backoff exponent."""
        self._rl_fail_count[llm_name] = self._rl_fail_count.get(llm_name, 0) + 1

    def on_success(self, llm_name: str) -> None:
        self._rl_fail_count[llm_name] = 0

    # ------------------------------------------------------------------
    # Quality windows + derived per-operation demotion verdicts
    # ------------------------------------------------------------------

    def wilson_bound(self, llm_name: str, operation: str | None) -> float | None:
        """The Wilson-score upper bound backing ``is_demoted``, for diagnostics."""
        window = self._scores.get((llm_name, operation))
        if not window:
            return None
        return wilson_upper(list(window), _z_score(self.quality_confidence))

    def is_demoted(self, llm_name: str, operation: str | None) -> bool:
        """True iff the window holds at least ``quality_min_count`` ratings and their
        Wilson-score upper bound sits below ``quality_floor``."""
        window = self._scores.get((llm_name, operation))
        if window is None or len(window) < self.quality_min_count:
            return False
        bound = wilson_upper(list(window), _z_score(self.quality_confidence))
        return bound < self.quality_floor

    def demoted_operations(self, llm_name: str) -> frozenset[str | None]:
        return frozenset(
            operation
            for name, operation in self._scores
            if name == llm_name and self.is_demoted(name, operation)
        )

    def record_quality(self, llm_name: str, operation: str | None, score: float) -> None:
        """Fold one rating into the (model, operation) window, oldest evicted first."""
        before = self.is_demoted(llm_name, operation)
        window = self._scores.setdefault(
            (llm_name, operation),
            deque(maxlen=self.quality_window),
        )
        window.append(score)
        self._log_flip(llm_name, operation, before, self.is_demoted(llm_name, operation))

    def load_scores(self, scores: dict[tuple[str, str | None], list[float]]) -> None:
        """Replace every window wholesale — used by the journal rebuild."""
        keys = set(self._scores) | set(scores)
        before = {key: self.is_demoted(*key) for key in keys}
        self._scores = {
            key: deque(values[-self.quality_window :], maxlen=self.quality_window)
            for key, values in scores.items()
        }
        for key in keys:
            self._log_flip(key[0], key[1], before[key], self.is_demoted(*key))

    def _log_flip(
        self,
        llm_name: str,
        operation: str | None,
        before: bool,  # noqa: FBT001
        after: bool,  # noqa: FBT001
    ) -> None:
        if before == after:
            return
        bound = self.wilson_bound(llm_name, operation)
        if after:
            if bound is not None:
                logger.warning(
                    "%s: quality-demoted for operation=%r (wilson upper %.3f < floor %.2f)",
                    llm_name,
                    operation,
                    bound,
                    self.quality_floor,
                )
            else:
                logger.warning("%s: quality-demoted for operation=%r", llm_name, operation)
        elif bound is not None:
            logger.info(
                "%s: quality demotion cleared for operation=%r (wilson upper %.3f)",
                llm_name,
                operation,
                bound,
            )
        else:
            logger.info("%s: quality demotion cleared for operation=%r", llm_name, operation)
