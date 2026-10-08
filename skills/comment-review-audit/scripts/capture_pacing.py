#!/usr/bin/env python3
"""Shared pacing helpers for serial platform capture."""

from __future__ import annotations

import random
from typing import Callable


def pause_seconds(
    elapsed_seconds: float,
    delay_min: float,
    delay_max: float,
    *,
    adaptive: bool = True,
    healthy: bool = True,
    minimum_rest: float = 1.5,
    chooser: Callable[[float, float], float] = random.uniform,
) -> float:
    """Return a safe inter-target pause without double-counting page work."""
    if delay_min < 0 or delay_max < delay_min or minimum_rest < 0:
        raise ValueError("invalid pacing bounds")
    target_interval = float(chooser(delay_min, delay_max))
    if not adaptive or not healthy:
        return target_interval
    return max(minimum_rest, target_interval - max(0.0, float(elapsed_seconds)))


def has_remaining_target(index: int, total: int) -> bool:
    """Indices are one-based in batch progress output."""
    return 0 < index < total
