"""UTF-8 subprocesses and bounded cleanup of an adapter's own process tree."""
from __future__ import annotations

import base64
import os
import signal
import subprocess
import sys


def child_environment(overrides=None):
    env = os.environ.copy()
    if overrides:
        env.update(overrides)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    return env


def configure_stdio():
    # Called only at CLI entry points, never when an SDK consumer imports us.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def powershell_command(script):
    prelude = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
    encoded = base64.b64encode((prelude + script).encode("utf-16-le")).decode("ascii")
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def process_group_options():
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate_process_tree(process, grace=3):
    """Only accept a Popen we created with process_group_options, not arbitrary PIDs."""
    if process.poll() is not None and all(stream is None or stream.closed
                                          for stream in (process.stdout, process.stderr)):
        # Cleanup can be called from finally after a prior successful cleanup.
        # Do not signal an old PID/process-group ID that might have been recycled.
        return
    if sys.platform == "win32":
        if process.poll() is None:
            # No /IM wildcards: ordinary user Chrome and unrelated jobs are untouched.
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        try:
            process.communicate(timeout=grace)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("owned_process_tree_cleanup_failed") from exc
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue
    raise RuntimeError("owned_process_tree_cleanup_failed")
