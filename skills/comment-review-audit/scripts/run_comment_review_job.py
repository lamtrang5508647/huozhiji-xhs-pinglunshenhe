#!/usr/bin/env python3
"""Run a resumable XLSX -> capture -> audit -> workbook comment-review job."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from decimal import Decimal
from hashlib import sha256
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from capture_resume import reusable_capture
from audit_validation import validate_results


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MAPS = (
    "target_url=文章链接",
    "product_name=产品",
    "comment_text=评论文字需求",
    "reply_text=需要回复评论内容【左边填写回第几条】",
    "image_requirement=评论图片需求",
    "expected_amount=金额【文字1.5，文字加图2元，单图1.5】",
)


def run(argv: list[str], env: dict[str, str] | None = None) -> None:
    print("+ " + " ".join(argv), flush=True)
    subprocess.run(argv, check=True, env=env)


def run_capture(argv: list[str]) -> None:
    while True:
        try:
            run(argv)
            return
        except subprocess.CalledProcessError as exc:
            # Exit 4 can only follow successful explicit owner confirmation.
            if exc.returncode != 4:
                raise
            print("[verification] owner-confirmed; resume saved checkpoint", flush=True)


def write_state(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_items(path: Path, key: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get(key, payload) if isinstance(payload, dict) else payload
    return items if isinstance(items, list) else []


def first_target(rows: Iterable[dict[str, Any]], platform: str) -> str:
    for row in rows:
        if row.get("platform") == platform and row.get("target_key"):
            return str(row["target_key"])
    return ""


def has_healthy_capture(observations: Iterable[dict[str, Any]], target_key: str) -> bool:
    return any(
        reusable_capture(item, "xiaohongshu", target_key)
        for item in observations
    )


def discover_node_modules(explicit: str | None) -> str:
    candidates = [explicit, os.environ.get("CODEX_NODE_MODULES"), os.environ.get("NODE_PATH")]
    candidates.extend(
        str(path)
        for path in sorted(
            (Path.home() / ".cache/codex-runtimes").glob("*/dependencies/node/node_modules"),
            reverse=True,
        )
    )
    for candidate in candidates:
        if candidate and (Path(candidate) / "@oai/artifact-tool").exists():
            return candidate
    raise RuntimeError("spreadsheet_runtime_not_found: pass --node-modules")


def write_manifest(path: Path, source: Path, output: Path, report: Path, expected: Path) -> None:
    payload = json.loads(report.read_text(encoding="utf-8"))
    results = payload.get("results", [])
    payout = validate_results(load_items(expected, "rows"), results)
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("final_workbook_missing")
    counts = {label: 0 for label in ("成功", "失败", "待人审")}
    for item in results:
        triage = item.get("triage", "待人审")
        counts[triage if triage in counts else "待人审"] += 1
    write_state(path, {
                "status": "complete",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "source": str(source.resolve()),
                "output": str(output.resolve()),
                "total": len(results),
                "success": counts["成功"],
                "failure": counts["失败"],
                "manual_review": counts["待人审"],
                "approved_amount": float(payout.quantize(Decimal("0.01"))),
                "source_sha256": sha256(source.read_bytes()).hexdigest(),
                "output_sha256": sha256(output.read_bytes()).hexdigest(),
            })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Incoming XLSX workbook")
    parser.add_argument("--work-dir", required=True, help="Checkpoint/output directory")
    parser.add_argument("--output-name", default="评论审核-最终版.xlsx")
    parser.add_argument("--map", action="append", dest="maps", help="Override/add destination=header mapping")
    parser.add_argument("--amount-rules")
    parser.add_argument("--profile", default=str(Path.home() / ".comment-review-audit/xhs-profile"))
    parser.add_argument("--node-modules")
    parser.add_argument("--workbook-engine", choices=("portable", "artifact"), default="portable",
                        help="Portable stdlib OOXML export (default); artifact enables host rendering")
    parser.add_argument("--headed", action="store_true", default=True)
    parser.add_argument("--normal-pace", action="store_true",
                        help="Opt out of the default low-risk capture profile for an explicitly approved legacy run")
    parser.add_argument("--wait-for-confirmation", action="store_true",
                        help="Hold actual verification pages until the owner confirms via stdin (use a PTY)")
    args = parser.parse_args()

    source = Path(args.source).resolve()
    work_dir = Path(args.work_dir).resolve()
    if not source.is_file() or source.suffix.lower() != ".xlsx":
        parser.error("--source must be an existing .xlsx file")
    if Path(args.output_name).name != args.output_name or not args.output_name.lower().endswith(".xlsx"):
        parser.error("--output-name must be an XLSX filename within --work-dir")
    work_dir.mkdir(parents=True, exist_ok=True)
    expected = work_dir / "expected.json"
    observations = work_dir / "observations.json"
    report = work_dir / "review-report.json"
    output = work_dir / args.output_name
    previews = work_dir / "previews"
    manifest = work_dir / "job-result.json"
    source_hash = sha256(source.read_bytes()).hexdigest()
    if manifest.exists():
        prior = json.loads(manifest.read_text(encoding="utf-8"))
        if prior.get("source_sha256") not in (None, source_hash):
            raise RuntimeError("source_changed: use a new job directory")
    write_state(manifest, {"status": "in_progress", "source": str(source), "source_sha256": source_hash})

    try:
        return execute_job(args, source, expected, observations, report, output, previews, manifest)
    except (subprocess.CalledProcessError, RuntimeError, OSError, ValueError) as exc:
        phase = "blocked" if isinstance(exc, subprocess.CalledProcessError) and exc.returncode in (2, 3) else "error"
        checkpoint = json.loads(observations.read_text(encoding="utf-8")) if observations.exists() else {}
        reason = checkpoint.get("stop_reason") or ("subprocess_failed" if isinstance(exc, subprocess.CalledProcessError) else str(exc))
        write_state(manifest, {"status": phase, "source": str(source), "source_sha256": source_hash,
                               "reason": reason, "checkpoint": str(observations)})
        print(f"[job] {phase}: {reason}; checkpoint retained", flush=True)
        return 3 if phase == "blocked" else 1


def execute_job(args, source, expected, observations, report, output, previews, manifest) -> int:

    convert = [
        sys.executable,
        str(SCRIPT_DIR / "table_to_expected.py"),
        "--input", str(source), "--output", str(expected), "--all-sheets",
    ]
    for mapping in args.maps or DEFAULT_MAPS:
        convert.extend(("--map", mapping))
    if args.amount_rules:
        convert.extend(("--amount-rules", args.amount_rules))
    run(convert)

    rows = load_items(expected, "rows")
    target = first_target(rows, "xiaohongshu")
    if not target:
        raise RuntimeError("no_xiaohongshu_target_found")
    captures = load_items(observations, "observations")
    checkpoint = json.loads(observations.read_text(encoding="utf-8")) if observations.exists() else {}
    restoring_session = checkpoint.get("stopped") is True
    if restoring_session:
        row_targets = {str(row.get("target_key")) for row in rows}
        target = next((str(item["target_key"]) for item in reversed(captures)
                       if item.get("capture_status") in ("captcha", "session_expired", "risk_control")
                       and str(item.get("target_key")) in row_targets), target)
    capture_base = [
        sys.executable,
        str(SCRIPT_DIR / "xhs_batch_capture.py"),
        "--input", str(source), "--output", str(observations),
        "--profile", args.profile, "--expand-dom", "--headed",
    ]
    if not args.normal_pace:
        capture_base.append("--risk-averse")
    if args.wait_for_confirmation:
        capture_base.append("--wait-for-confirmation")
    if restoring_session or not has_healthy_capture(captures, target):
        canary = capture_base + ["--only-note-id", target]
        if restoring_session:
            canary.append("--refresh")
        run_capture(canary)
        if not has_healthy_capture(load_items(observations, "observations"), target):
            write_state(manifest, {"status": "blocked", "source": str(source),
                                   "source_sha256": sha256(source.read_bytes()).hexdigest(),
                                   "reason": "canary_incomplete", "checkpoint": str(observations)})
            print("[canary] incomplete; inspect this target before batch capture", flush=True)
            return 3
    else:
        print(f"[canary] resume-skip healthy target {target}", flush=True)
    run_capture(capture_base)

    run([
        sys.executable,
        str(SCRIPT_DIR / "review_comments.py"),
        "--expected", str(expected), "--observations", str(observations),
        "--amount-mode", "expected-only", "--output", str(report),
    ])
    if args.workbook_engine == "portable":
        run([sys.executable, str(SCRIPT_DIR / "build_audited_workbook.py"),
             "--source", str(source), "--expected", str(expected),
             "--report", str(report), "--output", str(output)])
    else:
        node_modules = discover_node_modules(args.node_modules)
        env = os.environ.copy()
        env["CODEX_NODE_MODULES"] = node_modules
        run([
            "node", str(SCRIPT_DIR / "build_audited_workbook.mjs"),
            "--source", str(source), "--expected", str(expected),
            "--report", str(report), "--output", str(output),
            "--preview-dir", str(previews),
        ], env=env)
    write_manifest(manifest, source, output, report, expected)
    print(manifest.read_text(encoding="utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
