#!/usr/bin/env python3
"""Offline regression and deterministic speed checks for batch pacing."""

from capture_pacing import has_remaining_target, pause_seconds


def fixed(value: float):
    return lambda _minimum, _maximum: value


def main() -> None:
    assert pause_seconds(2.0, 6.0, 10.0, chooser=fixed(8.0)) == 6.0
    assert pause_seconds(12.0, 6.0, 10.0, chooser=fixed(8.0)) == 1.5
    assert pause_seconds(2.0, 6.0, 10.0, healthy=False, chooser=fixed(8.0)) == 8.0
    assert pause_seconds(2.0, 6.0, 10.0, adaptive=False, chooser=fixed(8.0)) == 8.0
    assert has_remaining_target(1, 2)
    assert not has_remaining_target(2, 2)

    # The real 8.27 sample has 25 XHS and 6 Douyin targets. With representative
    # healthy capture times, adaptive pacing removes over 40% of deterministic
    # idle time while retaining a rest floor and the existing scheduled breaks.
    old_idle = 25 * 8.0 + 2 * 35.0 + 6 * 6.5 + 6 * 3.5
    new_idle = 24 * pause_seconds(4.0, 6.0, 10.0, chooser=fixed(8.0)) + 2 * 35.0
    new_idle += 5 * pause_seconds(9.0, 5.0, 8.0, chooser=fixed(6.5))
    assert new_idle < old_idle * 0.60, (old_idle, new_idle)
    print(f"capture pacing tests passed: old_idle={old_idle:.1f}s new_idle={new_idle:.1f}s")


if __name__ == "__main__":
    main()
