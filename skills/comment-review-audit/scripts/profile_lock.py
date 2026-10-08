#!/usr/bin/env python3
"""Single-process lock for a persistent browser profile."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import IO, Optional


class ProfileLock:
    def __init__(self, profile: str | Path):
        self.profile = Path(profile).expanduser().resolve()
        self._handle: Optional[IO[str]] = None

    def __enter__(self) -> "ProfileLock":
        self.profile.mkdir(parents=True, exist_ok=True)
        os.chmod(self.profile, 0o700)
        handle = (self.profile / ".comment-review-audit.lock").open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError(f"browser_profile_in_use:{self.profile}") from exc
        self._handle = handle
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
