"""Run the suite as if on Windows: a monotonic clock that ticks every 15.6 ms.

Enabled with ``-p coarse_clock`` (``invoke test`` runs one pass with it). Windows'
``time.monotonic()`` comes from ``GetTickCount64()`` before Python 3.13, so two events
milliseconds apart carry the same instant there and any ordering taken from the clock
ties. This makes that platform reachable from a developer machine.
"""

import time
from types import SimpleNamespace

RESOLUTION = 0.015625

_monotonic = time.monotonic
_clock_info = time.get_clock_info


def _coarse() -> float:
    return (_monotonic() // RESOLUTION) * RESOLUTION


def _coarse_info(name: str):
    if name != "monotonic":
        return _clock_info(name)
    real = _clock_info(name)
    return SimpleNamespace(
        implementation="GetTickCount64()",
        monotonic=real.monotonic,
        adjustable=real.adjustable,
        resolution=RESOLUTION,
    )


def pytest_configure(config) -> None:
    # Both, or the halves disagree: the event loop reads the resolution to decide how
    # early a timer may fire, and tests/support.py reads it for CLOCK_SLACK.
    time.monotonic = _coarse
    time.get_clock_info = _coarse_info


def pytest_unconfigure(config) -> None:
    time.monotonic = _monotonic
    time.get_clock_info = _clock_info


def pytest_report_header(config) -> str:
    return f"clock: simulated Windows monotonic, {RESOLUTION * 1000:.1f} ms per tick"
