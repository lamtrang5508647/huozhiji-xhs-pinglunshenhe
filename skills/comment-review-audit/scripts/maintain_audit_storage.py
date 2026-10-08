#!/usr/bin/env python3
"""Archive and prune intermediate audit evidence older than a retention window."""

from __future__ import annotations

import argparse
import json
import os
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterable


PRESERVE_SUFFIXES = {".xlsx", ".xls", ".zip"}


def validate_root(raw: str) -> Path:
    root = Path(raw).expanduser().resolve()
    if root in {Path("/"), Path.home().resolve()} or len(root.parts) < 4:
        raise ValueError("refusing broad storage root")
    if not root.exists() or not root.is_dir():
        raise ValueError("storage root must be an existing directory")
    return root


def candidates(root: Path, cutoff: float) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file() or "archives" in path.relative_to(root).parts:
            continue
        if path.suffix.casefold() in PRESERVE_SUFFIXES or path.name.endswith(".keep"):
            continue
        try:
            if path.stat().st_mtime < cutoff:
                yield path
        except OSError:
            continue


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--retention-days", type=int, default=14)
    parser.add_argument("--archive-retention-days", type=int, default=14)
    parser.add_argument("--apply", action="store_true", help="Archive then remove candidates; default is dry-run")
    args = parser.parse_args()
    if args.retention_days < 1 or args.archive_retention_days < 1:
        parser.error("retention windows must be at least 1 day")
    try:
        root = validate_root(args.root)
    except ValueError as exc:
        parser.error(str(exc))
    cutoff = time.time() - args.retention_days * 86400
    found = sorted(candidates(root, cutoff))
    archive_dir = root / "archives"
    archive_cutoff = time.time() - args.archive_retention_days * 86400
    expired_archives = sorted(
        path for path in archive_dir.glob("audit-evidence-before-*.zip")
        if path.is_file() and path.stat().st_mtime < archive_cutoff
    ) if archive_dir.exists() else []
    payload = {
        "root": str(root),
        "retention_days": args.retention_days,
        "mode": "apply" if args.apply else "dry-run",
        "candidate_count": len(found),
        "candidate_bytes": sum(path.stat().st_size for path in found),
        "archive_retention_days": args.archive_retention_days,
        "expired_archive_count": len(expired_archives),
        "expired_archive_bytes": sum(path.stat().st_size for path in expired_archives),
        "archive": None,
        "removed_count": 0,
        "pruned_archive_count": 0,
    }
    if not args.apply or (not found and not expired_archives):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    archive = None
    if found:
        archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive = archive_dir / f"audit-evidence-before-{stamp}.zip"
        manifest = []
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
            for path in found:
                relative = path.relative_to(root)
                stat = path.stat()
                bundle.write(path, relative.as_posix())
                manifest.append({"path": relative.as_posix(), "size": stat.st_size, "mtime": stat.st_mtime})
            bundle.writestr("_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        with zipfile.ZipFile(archive) as bundle:
            if bundle.testzip() is not None:
                raise RuntimeError("archive verification failed; originals were preserved")
    removed = 0
    for path in found:
        path.unlink()
        removed += 1
    pruned_archives = 0
    for old_archive in expired_archives:
        old_archive.unlink()
        pruned_archives += 1
    for directory in sorted((path for path in root.rglob("*") if path.is_dir()), reverse=True):
        if directory == archive_dir:
            continue
        try:
            directory.rmdir()
        except OSError:
            pass
    payload["archive"] = str(archive) if archive else None
    payload["removed_count"] = removed
    payload["pruned_archive_count"] = pruned_archives
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
