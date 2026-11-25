#!/usr/bin/env python3
"""Merge observation JSON files, with later inputs replacing the same platform/target."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from screenshot_evidence import materialize_screenshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    merged = {}
    for input_path in args.input:
        payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
        rows = payload.get("observations", payload) if isinstance(payload, dict) else payload
        for row in rows:
            merged[(str(row.get("platform")), str(row.get("target_key")))] = (row, Path(input_path).resolve().parent)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    observations = []
    for (platform, key), (row, base) in merged.items():
        if "comment_screenshots" in row:
            source = row.get("comment_screenshots") or []
            files = [item for record in source if (item := materialize_screenshot(
                record, base, output.resolve().parent, platform, key)) is not None]
            row["comment_screenshots"] = files
            if not files or len(files) != len(source):
                row["screenshot_status"] = "partial" if files else "missing"
        observations.append(row)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"observations": observations}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
