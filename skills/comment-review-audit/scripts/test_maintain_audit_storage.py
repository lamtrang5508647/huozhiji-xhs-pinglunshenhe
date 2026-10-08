#!/usr/bin/env python3
"""Offline safety test for 14-day audit storage maintenance."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path


def run(*args: str) -> dict:
    output = subprocess.check_output([sys.executable, str(Path(__file__).with_name("maintain_audit_storage.py")), *args], text=True)
    return json.loads(output)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="comment-review-storage-") as directory:
        root = Path(directory) / "managed" / "outputs"
        root.mkdir(parents=True)
        old_json = root / "capture.json"
        old_xlsx = root / "final.xlsx"
        new_png = root / "recent.png"
        for path in (old_json, old_xlsx, new_png):
            path.write_text(path.name, encoding="utf-8")
        old = time.time() - 20 * 86400
        os.utime(old_json, (old, old))
        os.utime(old_xlsx, (old, old))
        dry = run("--root", str(root), "--retention-days", "14")
        assert dry["candidate_count"] == 1 and old_json.exists()
        applied = run("--root", str(root), "--retention-days", "14", "--apply")
        assert applied["removed_count"] == 1
        assert not old_json.exists() and old_xlsx.exists() and new_png.exists()
        with zipfile.ZipFile(applied["archive"]) as archive:
            assert "capture.json" in archive.namelist()
        archived = Path(applied["archive"])
        os.utime(archived, (old, old))
        prune_dry = run("--root", str(root), "--archive-retention-days", "14")
        assert prune_dry["expired_archive_count"] == 1 and archived.exists()
        pruned = run("--root", str(root), "--archive-retention-days", "14", "--apply")
        assert pruned["pruned_archive_count"] == 1 and not archived.exists()
    print("storage maintenance tests passed")


if __name__ == "__main__":
    main()
