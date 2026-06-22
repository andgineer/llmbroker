"""The ``Optimizer`` knob — P1 shape only; the autonomous control loop arrives in P4."""

from dataclasses import dataclass


@dataclass
class Optimizer:
    """P1 shape only — no control loop runs until P4."""

    judge_fraction: float = 0.0
