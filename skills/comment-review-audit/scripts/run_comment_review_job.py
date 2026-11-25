#!/usr/bin/env python3
"""Run a resumable XLSX -> capture -> audit -> workbook comment-review job."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from decimal import Decimal
from hashlib import sha256
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4
from xml.etree import ElementTree as ET
from audit_validation import validate_results
from audit_pass import rows_to_review, merge_audit_results
from capture_resume import PARSER_REVISIONS, recheck_scope
from capture_watchdog import DEFAULT_IDLE_SECONDS, IDLE_EXIT, positive_seconds
from build_audited_workbook import build_workbook
from profile_lock import default_profile
from review_comments import compare, unwrap_rows
from screenshot_evidence import DEFAULT_SCREENSHOT_DIR, attach_screenshots, screenshots_valid
from runtime_environment import child_environment, configure_stdio
from table_to_expected import convert_xlsx_sheets, parse_amount_rules, parse_maps
from xhs_batch_capture import SINGLE_NOTE_PAGE_STATES, capture_covers_tasks, group_note_requirements, has_visible_comment, plan_targets, target_from_url


SCRIPT_DIR = Path(__file__).resolve().parent


def run(argv: list[str], env: dict[str, str] | None = None) -> None:
    print("+ " + " ".join(argv), flush=True)
    subprocess.run(argv, check=True, env=child_environment(env))


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


def read_checkpoint(path: Path) -> dict:
    if not path.exists():
        return {"observations": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    # Older evidence files are bare arrays. Keep stop metadata on newer files.
    if isinstance(payload, list):
        return {"observations": payload}
    if not isinstance(payload, dict):
        raise ValueError("invalid_observations_checkpoint")
    return payload


def first_target(rows: Iterable[dict[str, Any]], platform: str) -> str:
    for row in rows:
        if row.get("platform") == platform and row.get("target_key"):
            return str(row["target_key"])
    return ""


def last_capture(observations, target):
    return next((item for item in reversed(observations) if item.get("platform") == "xiaohongshu"
                 and str(item.get("target_key")) == target), None)


def latest_note_observations(checkpoint):
    # Planning uses the last capture written for each note. Use exactly that same view
    # when judging the live job, including on resume from older multi-snapshot files.
    captures = unwrap_rows(checkpoint, "observations")
    return list({(c.get("platform"), str(c.get("target_key"))): c for c in captures}.values())


def has_healthy_capture(observations: list[dict[str, Any]], target_key: str, rows=(), *, context_rows=None, screenshot_dir=None) -> bool:
    item = last_capture(observations, target_key)
    return (capture_covers_tasks(item, target_key, rows, context_rows=context_rows)
            and (screenshot_dir is None or screenshots_valid(item, screenshot_dir)))


def readable_recheck_probe(item, key, rows, started_at, *, recheck=True):
    """Identify a fresh target-specific visit, independent of whether its tasks were resolved."""
    if (not item or item.get("platform") != "xiaohongshu" or item.get("target_key") != key
            or item.get("parser_revision") != PARSER_REVISIONS["xiaohongshu"]):
        return False
    local_problem = item.get("capture_status") == "inaccessible" and item.get("target_page_state") in SINGLE_NOTE_PAGE_STATES
    readable = (recheck and item.get("capture_status") == "ok" and item.get("visible_comment_panel_readable") is True
                and has_visible_comment(item.get("comments") or []))
    if not (local_problem or readable):
        return False
    resolved = item.get("resolved_target_key")
    if not resolved:
        return False
    if resolved != key:
        # Only the platform's short links resolve to a different canonical note ID.
        sources = (target_from_url(row.get("target_url", "")) for row in rows)
        if not any(link and link.get("short_url") and link["note_id"] == key for link in sources):
            return False
    try:
        captured_at = datetime.fromisoformat(item["captured_at"].replace("Z", "+00:00"))
        return started_at <= captured_at <= datetime.now(timezone.utc)
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def checkpoint_covers_workbook(rows, checkpoint, requirements, contexts=None, *, screenshot_dir=None):
    if checkpoint.get("stopped"):
        return False
    captures = unwrap_rows(checkpoint, "observations")
    if not captures:
        return False
    existing = {str(item.get("target_key")): item for item in captures if item.get("platform") == "xiaohongshu"}
    keys = dict.fromkeys(str(row["target_key"]) for row in rows
                         if row.get("platform") == "xiaohongshu" and row.get("target_key"))
    targets = [{"note_id": key} for key in keys]
    if screenshot_dir is not None and any(not screenshots_valid(existing.get(key), screenshot_dir) for key in keys):
        return False
    return bool(targets) and not plan_targets(targets, existing, requirements, contexts=contexts)


def capture_progress(rows, checkpoint):
    """Inventory attempts, including blocked notes. Counts are not publication verdicts."""
    attempted = {(item.get("platform"), str(item.get("target_key")))
                 for item in unwrap_rows(checkpoint, "observations")}
    tasks = [(row.get("platform"), str(row.get("target_key"))) for row in rows]
    return {"task_rows": len(tasks), "unique_targets": len(set(tasks)),
            "rows_with_capture_record": sum(key in attempted for key in tasks),
            "rows_without_capture_record": sum(key not in attempted for key in tasks)}


def retry_incomplete_notes(rows, checkpoint, report, path, capture_command, already_retried, *, context_rows=None):
    """One additional visit per unresolved note, retaining stronger first-pass evidence."""
    captures = unwrap_rows(checkpoint, "observations")
    current = {str(item["target_key"]): item for item in captures if item.get("platform") == "xiaohongshu"}
    retry = {}
    if "--screenshot-dir" in capture_command:
        for row in rows:
            key = row.get("target_key")
            item = current.get(key, {})
            if (key not in already_retried and row.get("platform") == "xiaohongshu" and item.get("capture_status") == "ok"
                    and item.get("screenshot_status") in ("partial", "missing")):
                retry[key] = []
    for result in report["results"]:
        key = result["target_key"]
        if result["platform"] != "xiaohongshu" or result["triage"] != "待人审" or key in already_retried:
            continue
        record = current.get(key, {})
        if record.get("capture_status") in ("captcha", "session_expired", "risk_control", "network_changed"):
            continue
        if record.get("capture_complete") is True and record.get("image_detection_complete") is not False:
            continue
        if result["status"] == "blocked" or set(result["reason_codes"]).intersection(
                {"comment_image_presence_unknown", "reply_body_prefix_unconfirmed"}):
            retry[key] = []
    if not retry:
        return checkpoint, report
    for row in rows:
        if row.get("platform") == "xiaohongshu" and row.get("target_key") in retry:
            retry[row["target_key"]].append(row)
    settled = {key: set() for key in retry}
    for result in report["results"]:
        if result["platform"] == "xiaohongshu" and result["target_key"] in retry and result["triage"] == "成功":
            settled[result["target_key"]].add(result["case_id"])
    with tempfile.TemporaryDirectory(dir=path.parent, prefix="capture-recheck-") as directory:
        fresh_path = Path(directory) / "observations.json"
        command = capture_command.copy()
        command[command.index("--output") + 1] = str(fresh_path)
        for key in retry:
            command.extend(["--only-note-id", key])
        try:
            run_capture(command)
        finally:
            fresh = read_checkpoint(fresh_path)
            replacements = {}
            for item in unwrap_rows(fresh, "observations"):
                key = str(item.get("target_key"))
                if item.get("platform") != "xiaohongshu" or key not in retry:
                    continue
                if current.get(key, {}).get("capture_status") == "ok" and item.get("capture_status") != "ok":
                    continue
                if settled[key]:
                    checked = compare(retry[key], [item], amount_mode="expected-only",
                                      reply_context_rows=context_rows)["results"]
                    if not settled[key].issubset(r["case_id"] for r in checked if r["triage"] == "成功"):
                        continue
                replacements[key] = item
            kept = [c for c in captures if c.get("platform") != "xiaohongshu" or str(c.get("target_key")) not in replacements]
            checkpoint = {**checkpoint, **fresh, "observations": kept + list(replacements.values())}
            if fresh.get("stopped"):
                stopped = next((c for c in reversed(fresh.get("observations", []))
                                if c.get("capture_status") in ("captcha", "session_expired", "risk_control")), {})
                checkpoint["stop_target_key"] = stopped.get("target_key")
            write_state(path, checkpoint)
    return checkpoint, compare(rows, latest_note_observations(checkpoint), amount_mode="expected-only", reply_context_rows=context_rows)


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


def write_manifest(path: Path, source: Path, output: Path, rows, results, workbook, summary, pass_state) -> None:
    payout = validate_results(rows, results)
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("final_workbook_missing")
    counts = {label: 0 for label in ("成功", "失败", "待人审")}
    for item in results:
        triage = item.get("triage", "待人审")
        counts[triage] = counts.get(triage, 0) + 1
    write_state(path, {
                "status": "complete",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "source": str(source.resolve()),
                "output": str(output.resolve()),
                "total": len(results),
                "success": counts["成功"],
                "failure": counts["失败"],
                "manual_review": counts["待人审"],
                **({"unreviewed_rows": counts["未复核"]} if counts.get("未复核") else {}),
                **({"settlement_pending_count": summary["settlement_pending_count"]} if summary.get("settlement_pending_count") else {}),
                "audit_pass": summary["audit_pass"], **pass_state,
                **({"screenshot_evidence": summary["screenshot_evidence"]} if "screenshot_evidence" in summary else {}),
                "approved_amount": float(payout.quantize(Decimal("0.01"))),
                "source_sha256": workbook["source_sha256"] if workbook else sha256(source.read_bytes()).hexdigest(),
                "output_sha256": workbook["output_sha256"] if workbook else sha256(output.read_bytes()).hexdigest(),
            })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Incoming XLSX workbook")
    parser.add_argument("--work-dir", required=True, help="Checkpoint/output directory")
    parser.add_argument("--output-name", default="评论审核-最终版.xlsx")
    parser.add_argument("--map", action="append", dest="maps", help="Override/add destination=header mapping")
    parser.add_argument("--amount-rules")
    parser.add_argument("--profile", default=str(default_profile("xhs")))
    parser.add_argument("--capture-idle-timeout", type=positive_seconds, default=DEFAULT_IDLE_SECONDS, metavar="SECONDS",
                        help="Stop capture after this long without progress, excluding owner input and scheduled breaks (default: 300)")
    parser.add_argument("--node-modules")
    parser.add_argument("--workbook-engine", choices=("portable", "artifact"), default="portable",
                        help="Portable stdlib OOXML export (default); artifact enables host rendering")
    parser.add_argument("--headed", action="store_true", default=True)
    parser.add_argument("--normal-pace", action="store_true",
                        help="Opt out of the default low-risk capture profile for an explicitly approved legacy run")
    parser.add_argument("--wait-for-confirmation", action="store_true",
                        help="Hold actual verification pages until the owner confirms via stdin (use a PTY)")
    parser.add_argument("--no-screenshots", action="store_true", help="Skip saving comment screenshots for this job")
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
    if output.resolve() == source or (output.exists() and output.samefile(source)):
        parser.error("use a different --output-name or a new --work-dir to preserve the input workbook")
    previews = work_dir / "previews"
    manifest = work_dir / "job-result.json"
    source_hash = sha256(source.read_bytes()).hexdigest()
    prior = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    if prior.get("source_sha256") not in (None, source_hash):
        parser.error("source_changed: use a new --work-dir to preserve the earlier audit")
    pass_state = {}
    active = []

    try:
        rows = convert_xlsx_sheets(source, parse_maps(args.maps or []), parse_amount_rules(args.amount_rules),
                                   skip_summary=True)
        if not rows:
            raise ValueError("expected_rows_empty")
        active = rows_to_review(rows)
        if any(r.get("platform") != "xiaohongshu" for r in active):
            raise ValueError("run supports Xiaohongshu tasks only: use the appropriate capture-xhs and "
                             "capture-douyin commands, merge observations, then run audit on the workbook")
        scope = recheck_scope(rows)
        if scope:
            if prior.get("status") == "complete" and not prior.get("recheck_id") and output.exists():
                parser.error("use a new --work-dir to preserve the first audit")
            previous_id = str(prior.get("recheck_id", ""))
            can_resume = (prior.get("status") in ("in_progress", "blocked", "error") and
                          prior.get("recheck_scope") == scope and len(previous_id) == 32 and
                          all(letter in "0123456789abcdef" for letter in previous_id))
            if can_resume:
                previous_path = work_dir / f"recheck-observations-{previous_id}.json"
                if previous_path.exists() and read_checkpoint(previous_path).get("recheck_scope") != scope:
                    can_resume = False
            pass_id = previous_id if can_resume else uuid4().hex
            observations = work_dir / f"recheck-observations-{pass_id}.json"
            pass_state = {"recheck_scope": scope, "recheck_id": pass_id, "checkpoint": str(observations)}
        write_state(manifest, {"status": "in_progress", "source": str(source), "source_sha256": source_hash, **pass_state})
        write_state(expected, {"rows": rows})
        if active:
            selected = collect_rows(args, source, expected, observations, manifest, active, pass_state,
                                    context_rows=rows if scope else None)
            if selected is None:
                return 3
        else:
            selected = compare([], [], amount_mode="expected-only")
            write_state(observations, {"observations": [], "stopped": False, "recheck_scope": scope})
        payload = merge_audit_results(rows, selected)
        if not args.no_screenshots and active:
            attach_screenshots(payload, latest_note_observations(read_checkpoint(observations)),
                               work_dir, enabled=True)
        return export_job(args, source, expected, report, output, previews, manifest, rows, payload, pass_state)
    except (subprocess.CalledProcessError, RuntimeError, OSError, ValueError, KeyError,
            ArithmeticError, zipfile.BadZipFile, ET.ParseError) as exc:
        phase = "blocked" if isinstance(exc, subprocess.CalledProcessError) and exc.returncode in (2, 3, IDLE_EXIT) else "error"
        reason = str(exc)
        if isinstance(exc, subprocess.CalledProcessError):
            reason = ("capture_idle_timeout" if exc.returncode == IDLE_EXIT else
                      read_checkpoint(observations).get("stop_reason") or "subprocess_failed")
        state = {"status": phase, "source": str(source), "source_sha256": source_hash,
                 "reason": reason, "checkpoint": str(observations), **pass_state}
        if phase == "blocked" and expected.exists():
            state["coverage"] = capture_progress(active, read_checkpoint(observations))
        write_state(manifest, state)
        print(f"[job] {phase}: {reason}; checkpoint retained", flush=True)
        return 3 if phase == "blocked" else 1


def collect_rows(args, source, expected, observations, manifest, rows, pass_state, *, context_rows=None):
    target = first_target(rows, "xiaohongshu")
    if not target:
        raise RuntimeError("no_xiaohongshu_target_found")
    checkpoint = read_checkpoint(observations)
    captures = unwrap_rows(checkpoint, "observations")
    requirements = group_note_requirements(rows)
    contexts = group_note_requirements(context_rows or ())
    restoring_session = checkpoint.get("stopped") is True
    if restoring_session:
        row_targets = {str(row.get("target_key")) for row in rows}
        target = next((str(item["target_key"]) for item in reversed(captures)
                       if item.get("capture_status") in ("captcha", "session_expired", "risk_control")
                       and str(item.get("target_key")) in row_targets), target)
        if checkpoint.get("stop_target_key") in row_targets:
            target = checkpoint["stop_target_key"]
    capture_base = [
        sys.executable,
        str(SCRIPT_DIR / "xhs_batch_capture.py"),
        "--input", str(source), "--output", str(observations), "--expected", str(expected),
        "--task-targets", "--profile", args.profile, "--expand-dom", "--headed",
    ]
    screenshot_dir = None if getattr(args, "no_screenshots", False) else observations.parent / DEFAULT_SCREENSHOT_DIR
    capture_base.extend(["--screenshot-dir", str(screenshot_dir)] if screenshot_dir is not None else ["--no-screenshots"])
    for mapping in args.maps or ():
        capture_base.extend(["--map", mapping])
    if not args.normal_pace:
        capture_base.append("--risk-averse")
    if args.wait_for_confirmation:
        capture_base.append("--wait-for-confirmation")
    capture_base = [sys.executable, str(SCRIPT_DIR / "capture_watchdog.py"), str(args.capture_idle_timeout), *capture_base]
    already_retried = set()
    probed_partial = set()

    def probe(key, *, refresh=False):
        command = capture_base + ["--only-note-id", key]
        if refresh:
            command.append("--refresh")
        started = datetime.now(timezone.utc)
        run_capture(command)
        updated = read_checkpoint(observations)
        item = last_capture(unwrap_rows(updated, "observations"), key)
        healthy = not updated.get("stopped") and capture_covers_tasks(
            item, key, requirements.get(key), context_rows=contexts.get(key))
        if healthy and screenshot_dir and item.get("screenshot_status") in ("partial", "missing"):
            probed_partial.add(key)
        if (not healthy and not updated.get("stopped") and readable_recheck_probe(
                item, key, requirements.get(key, ()), started, recheck=bool(pass_state.get("recheck_id")))):
            # This visit already read the page. Defer any missing tasks to the bounded final retry.
            probed_partial.add(key)
            healthy = True
        return updated, healthy

    if restoring_session or not has_healthy_capture(captures, target, requirements.get(target),
                                                   context_rows=contexts.get(target), screenshot_dir=screenshot_dir):
        checkpoint, healthy = probe(target, refresh=restoring_session)
        if not healthy and not restoring_session and not checkpoint.get("stopped"):
            # An isolated inaccessible or incomplete post cannot establish session health.
            # Try one other assigned note before blocking the whole workbook.
            alternate = next((row["target_key"] for row in rows if row.get("platform") == "xiaohongshu"
                              and row.get("target_key") and row["target_key"] != target), None)
            if alternate:
                already_retried.add(target)
                checkpoint, healthy = probe(str(alternate), refresh=True)
        if not healthy:
            write_state(manifest, {"status": "blocked", "source": str(source),
                                   "source_sha256": sha256(source.read_bytes()).hexdigest(),
                                   "reason": "canary_incomplete", "checkpoint": str(observations),
                                   "coverage": capture_progress(rows, checkpoint), **pass_state})
            print("[canary] incomplete; inspect this target before batch capture", flush=True)
            return None
    else:
        print(f"[canary] resume-skip healthy target {target}", flush=True)
    batch_rows = [r for r in rows if r.get("target_key") not in probed_partial] if probed_partial else rows
    if batch_rows and not checkpoint_covers_workbook(batch_rows, checkpoint, requirements, contexts=contexts, screenshot_dir=screenshot_dir):
        command = capture_base.copy()
        for key in sorted(probed_partial):
            command.extend(["--skip-note-id", key])
        run_capture(command)
        checkpoint = read_checkpoint(observations)

    payload = compare(rows, latest_note_observations(checkpoint), amount_mode="expected-only", reply_context_rows=context_rows)
    checkpoint, payload = retry_incomplete_notes(rows, checkpoint, payload, observations, capture_base, already_retried,
                                                context_rows=context_rows)
    return payload


def export_job(args, source, expected, report, output, previews, manifest, rows, payload, pass_state):
    write_state(report, payload)
    workbook = None
    if args.workbook_engine == "portable":
        workbook = build_workbook(source, rows, payload["results"], output)
    else:
        node_modules = discover_node_modules(args.node_modules)
        env = os.environ.copy()
        env["CODEX_NODE_MODULES"] = node_modules
        env["AUDIT_PYTHON"] = sys.executable
        run([
            "node", str(SCRIPT_DIR / "build_audited_workbook.mjs"),
            "--source", str(source), "--expected", str(expected),
            "--report", str(report), "--output", str(output),
            "--preview-dir", str(previews),
        ], env=env)
    write_manifest(manifest, source, output, rows, payload["results"], workbook, payload["summary"], pass_state)
    print(manifest.read_text(encoding="utf-8"), end="")
    return 0


if __name__ == "__main__":
    configure_stdio()
    raise SystemExit(main())
