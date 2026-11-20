"""Stop an unattended capture when neither its checkpoint nor its visible evidence advances."""
from __future__ import annotations

import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if __package__:
    from .runtime_environment import child_environment, process_group_options, terminate_process_tree
else:
    from runtime_environment import child_environment, process_group_options, terminate_process_tree


DEFAULT_IDLE_SECONDS = 300
IDLE_EXIT = 5
PROGRESS_ENV = "COMMENT_AUDIT_CAPTURE_PROGRESS"


def visible_progress() -> None:
    marker = os.environ.get(PROGRESS_ENV)
    if marker:
        # An append remains observable even on file systems with coarse timestamps.
        with open(marker, "ab") as receipt:
            receipt.write(b".")


def paced_sleep(seconds: float) -> None:
    marker = os.environ.get(PROGRESS_ENV)
    if marker and math.isfinite(seconds) and seconds > 0:
        # An intentionally quiet interval has an explicit end. Give capture its
        # normal idle budget after that point, even if a custom break is long.
        pause = Path(marker).with_name("pause-until")
        pause.write_text(str(time.monotonic() + seconds), encoding="ascii")
    time.sleep(seconds)


def positive_seconds(value) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("capture idle timeout must be finite and positive")
    return seconds


def checkpoint_version(path):
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_mtime_ns, info.st_size, info.st_ino


def supervise(command, checkpoint: Path, idle_seconds: float, *, stdout=None) -> int:
    idle_seconds = positive_seconds(idle_seconds)
    with tempfile.TemporaryDirectory(prefix="capture-progress-") as directory:
        marker = Path(directory, "progress")
        pause = Path(directory, "pause-until")
        last = checkpoint_version(checkpoint), checkpoint_version(marker)
        env = child_environment({PROGRESS_ENV: str(marker.resolve())})
        process = subprocess.Popen(command, env=env, stdout=stdout, **process_group_options())
        try:
            deadline = time.monotonic() + idle_seconds
            while process.poll() is None:
                current = checkpoint_version(checkpoint), checkpoint_version(marker)
                if current != last:
                    last = current
                    deadline = time.monotonic() + idle_seconds
                try:
                    pause_until = float(pause.read_text(encoding="ascii"))
                except (FileNotFoundError, ValueError):
                    pass
                else:
                    deadline = max(deadline, pause_until + idle_seconds)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    print(f"[capture] no checkpoint or new visible evidence for {idle_seconds:g}s; stopping", file=sys.stderr, flush=True)
                    return IDLE_EXIT
                try:
                    return process.wait(timeout=min(0.5, idle_seconds / 4, remaining))
                except subprocess.TimeoutExpired:
                    continue
            return process.returncode
        finally:
            if process.poll() is None:
                terminate_process_tree(process)


if __name__ == "__main__":
    # Keep all capture flags together so retries with a temporary --output monitor their own file.
    captured_command = sys.argv[2:]
    destination = Path(captured_command[captured_command.index("--output") + 1])
    raise SystemExit(supervise(captured_command, destination, float(sys.argv[1])))
