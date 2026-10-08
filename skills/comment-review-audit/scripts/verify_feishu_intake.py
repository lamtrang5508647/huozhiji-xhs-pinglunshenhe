#!/usr/bin/env python3
"""Verify that OpenClaw can accept image-heavy Feishu audit workbooks."""

from __future__ import annotations

import argparse
import subprocess


DEFAULT_OPENCLAW_MEDIA_MAX_MB = 30
REQUIRED_MEDIA_MAX_MB = 100


def parse_media_max_mb(raw: str) -> int:
    value = raw.strip()
    if not value:
        return DEFAULT_OPENCLAW_MEDIA_MAX_MB
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"invalid Feishu mediaMaxMb value: {value!r}") from exc
    if parsed <= 0:
        raise ValueError("Feishu mediaMaxMb must be positive")
    return parsed


def validate_media_max_mb(value: int, minimum: int = REQUIRED_MEDIA_MAX_MB) -> None:
    if value < minimum:
        raise ValueError(
            f"Feishu inbound attachment limit is {value} MB; "
            f"comment-audit workbooks require at least {minimum} MB"
        )


def read_openclaw_media_max_mb() -> int:
    result = subprocess.run(
        ["openclaw", "config", "get", "channels.feishu.mediaMaxMb"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # An unset key uses OpenClaw's built-in 30 MB default.
        return DEFAULT_OPENCLAW_MEDIA_MAX_MB
    return parse_media_max_mb(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--value", type=int, help="override config lookup for testing")
    parser.add_argument("--minimum", type=int, default=REQUIRED_MEDIA_MAX_MB)
    args = parser.parse_args()

    value = args.value if args.value is not None else read_openclaw_media_max_mb()
    validate_media_max_mb(value, args.minimum)
    print(f"Feishu inbound attachment limit: {value} MB (required: {args.minimum} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
