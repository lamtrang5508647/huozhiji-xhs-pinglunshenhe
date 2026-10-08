#!/usr/bin/env python3
"""Non-secret browser/network stability helpers for read-only audit sessions."""

from __future__ import annotations

import hashlib
import subprocess


def _command_output(argv: list[str]) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=3, check=False)
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def network_fingerprint() -> str:
    """Hash route/proxy state without storing addresses or credentials."""
    route = _command_output(["route", "-n", "get", "default"])
    route_lines = [
        line.strip() for line in route.splitlines()
        if line.strip().startswith(("interface:", "gateway:"))
    ]
    proxy = _command_output(["scutil", "--proxy"])
    proxy_lines = [
        line.strip() for line in proxy.splitlines()
        if line.strip().startswith(("HTTPEnable", "HTTPSEnable", "SOCKSEnable"))
    ]
    payload = "\n".join(route_lines + proxy_lines)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest() if payload else "unavailable"
