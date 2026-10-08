#!/usr/bin/env python3
"""Non-secret browser/network stability helpers for read-only audit sessions."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

from runtime_environment import powershell_command


def _command_output(argv: list[str]) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace", timeout=10, check=False)
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def network_fingerprint() -> str:
    """Hash route/proxy state without storing addresses or credentials."""
    env_proxy = {key: value for key, value in os.environ.items()
                 if key.casefold() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    if sys.platform == "win32":
        script = r"""
$routes = @(Get-NetRoute -PolicyStore ActiveStore | Where-Object {
  $_.DestinationPrefix -in @('0.0.0.0/0','::/0','0.0.0.0/1','128.0.0.0/1','::/1','8000::/1')
} | Sort-Object DestinationPrefix, InterfaceIndex, NextHop, RouteMetric |
  Select-Object DestinationPrefix, InterfaceIndex, NextHop, RouteMetric, InterfaceMetric)
if ($routes.Count -eq 0) { exit 1 }
$proxy = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
$connections = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings\Connections' -ErrorAction SilentlyContinue
[ordered]@{routes=$routes; proxy=($proxy | Select-Object ProxyEnable, ProxyServer, ProxyOverride, AutoConfigURL);
 connections=$connections.DefaultConnectionSettings} | ConvertTo-Json -Depth 5 -Compress
"""
        payload = _command_output(powershell_command(script))
        try:
            state = json.loads(payload)
            if not isinstance(state, dict) or not state.get("routes"):
                return "unavailable"
            state["environment_proxy"] = env_proxy
            payload = json.dumps(state, sort_keys=True, separators=(",", ":"))
        except (ValueError, TypeError):
            return "unavailable"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if sys.platform.startswith("linux"):
        payload = _command_output(["ip", "route", "show", "default"])
        if payload:
            payload += json.dumps(env_proxy, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest() if payload else "unavailable"
    route = _command_output(["route", "-n", "get", "default"])
    route_lines = [
        line.strip() for line in route.splitlines()
        if line.strip().startswith(("interface:", "gateway:"))
    ]
    proxy = _command_output(["scutil", "--proxy"])
    proxy_lines = [
        line.strip() for line in proxy.splitlines()
        if line.strip().startswith(("HTTP", "HTTPS", "SOCKS", "ProxyAuto", "ExceptionsList", "ExcludeSimpleHostnames"))
    ]
    payload = "\n".join(route_lines + proxy_lines)
    return hashlib.sha256((payload + json.dumps(env_proxy, sort_keys=True)).encode("utf-8")).hexdigest() if route_lines else "unavailable"
