#!/usr/bin/env python3
"""Offline tests for resumable job orchestration helpers."""

import tempfile
import json
import subprocess
import sys
from decimal import Decimal
from unittest.mock import patch
from datetime import datetime, timezone
from capture_resume import PARSER_REVISIONS
from pathlib import Path

from run_comment_review_job import discover_node_modules, first_target, has_healthy_capture, main as run_job, run_capture, validate_results


def main() -> None:
    rows = [
        {"platform": "douyin", "target_key": "d1"},
        {"platform": "xiaohongshu", "target_key": "x1"},
    ]
    assert first_target(rows, "xiaohongshu") == "x1"
    assert not has_healthy_capture([], "x1")
    assert not has_healthy_capture(
        [{"target_key": "x1", "capture_status": "ok", "capture_complete": False}], "x1"
    )
    assert has_healthy_capture(
        [{"platform":"xiaohongshu", "target_key": "x1", "capture_status": "ok", "capture_complete": True,
          "captured_at":datetime.now(timezone.utc).isoformat(), "comments":[],
          "parser_revision":PARSER_REVISIONS['xiaohongshu']}], "x1"
    )
    with tempfile.TemporaryDirectory() as temp:
        modules = Path(temp)
        (modules / "@oai/artifact-tool").mkdir(parents=True)
        assert discover_node_modules(str(modules)) == str(modules)
    rows = [{"case_id": "a"}, {"case_id": "b"}]
    results = [
        {"case_id": "a", "triage": "成功", "result_category": "成功", "approved_amount": "0.10"},
        {"case_id": "b", "triage": "成功", "result_category": "成功", "approved_amount": "0.20"},
    ]
    assert validate_results(rows, results) == Decimal("0.30")
    for broken in ([results[0]], [results[0], results[0]],
                   [results[0], {**results[1], "triage": "待人审"}],
                   [results[0], {**results[1], "triage": "失败"}]):
        try:
            validate_results(rows, broken)
        except RuntimeError:
            pass
        else:
            raise AssertionError("invalid report accepted")
    with patch("run_comment_review_job.run", side_effect=[subprocess.CalledProcessError(4, ["capture"]), None]) as mocked:
        run_capture(["capture"])
        assert mocked.call_count == 2
    with patch("run_comment_review_job.run", side_effect=subprocess.CalledProcessError(3, ["capture"])) as mocked:
        try:
            run_capture(["capture"])
        except subprocess.CalledProcessError:
            pass
        assert mocked.call_count == 1
    with tempfile.TemporaryDirectory() as temp:
        folder = Path(temp)
        source = folder / "fixture.xlsx"
        source.write_bytes(b"offline fixture")
        job = folder / "job"
        job.mkdir()
        (job / "job-result.json").write_text('{"status":"complete"}')
        def simulate(argv, env=None):
            if "table_to_expected.py" in argv[1]:
                (job / "expected.json").write_text(json.dumps({"rows": [{"case_id": "a", "platform": "xiaohongshu", "target_key": "x1"}]}))
            else:
                (job / "observations.json").write_text(json.dumps({"observations": [{"platform":"xiaohongshu", "target_key":"x1", "capture_status":"ok", "capture_complete":False}]}))
        args = ["job", "--source", str(source), "--work-dir", str(job)]
        with patch.object(sys, "argv", args), patch("run_comment_review_job.run", side_effect=simulate) as mocked:
            assert run_job() == 3
            assert mocked.call_count == 2  # incomplete canary never starts the full batch
            assert json.loads((job / "job-result.json").read_text())["status"] == "blocked"
    print("comment review job tests passed")


if __name__ == "__main__":
    main()
