#!/usr/bin/env python3
"""Offline test for persistent-profile single-process protection."""

from __future__ import annotations

import tempfile
import sys
from pathlib import Path

from profile_lock import ProfileLock


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="comment-review-profile-") as directory:
        profile = Path(directory) / "profile"
        with ProfileLock(profile):
            try:
                with ProfileLock(profile):
                    raise AssertionError("second lock unexpectedly succeeded")
            except RuntimeError as exc:
                assert str(exc).startswith("browser_profile_in_use:")
        with ProfileLock(profile):
            if sys.platform != "win32":
                assert profile.stat().st_mode & 0o777 == 0o700
    print("profile lock tests passed")


if __name__ == "__main__":
    main()
