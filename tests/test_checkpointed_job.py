import json
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from test_portable_api import fixture

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/comment-review-audit/scripts"
sys.path.insert(0, str(SCRIPTS))
import run_comment_review_job as runner
from capture_resume import PARSER_REVISIONS


class CheckpointedJobTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.job = Path(temp.name)
        self.source = self.job / "测试.xlsx"
        self.checkpoint = fixture(self.source)
        self.rows = runner.convert_xlsx_sheets(self.source, {}, None)
        self.now = datetime.now(timezone.utc)
        for item in self.checkpoint["observations"]:
            item.update(captured_at=self.now.isoformat(), parser_revision=PARSER_REVISIONS["xiaohongshu"],
                        capture_complete=True)

    def test_every_workbook_target_needs_a_healthy_latest_capture(self):
        self.assertTrue(runner.checkpoint_covers_workbook(self.rows, self.checkpoint, {}))
        self.assertFalse(runner.checkpoint_covers_workbook(self.rows, {**self.checkpoint, "stopped": True}, {}))
        captures = self.checkpoint["observations"]
        self.assertFalse(runner.checkpoint_covers_workbook(self.rows, {"observations": captures[:-1]}, {}))
        for change in ({"image_detection_complete": False}, {"parser_revision": "old"},
                       {"capture_status": "captcha"}, {"capture_complete": False},
                       {"captured_at": (self.now - timedelta(days=2)).isoformat()}):
            with self.subTest(change=change):
                latest = [*captures, {**captures[0], **change}]
                self.assertFalse(runner.has_healthy_capture(latest, captures[0]["target_key"]))
                self.assertFalse(runner.checkpoint_covers_workbook(self.rows, {"observations": latest}, {}))

    def test_live_job_reuses_visible_reply_without_parent_even_when_only_positives_were_captured(self):
        captures = self.checkpoint["observations"]
        root = dict(platform="xiaohongshu", target_key="n1", source_sheet="S", source_row=2,
                    case_id="root", comment_text="parent", expected_amount="1.00")
        reply = dict(root, source_row=3, case_id="reply", comment_text="child", reply_to="回复第1条")
        rows = [root, reply]
        comments = [dict(text="parent", comment_id="r", parent_comment_id=None, has_image=False),
                    dict(text="child", comment_id="c", has_image=False)]
        captures[0]["comments"] = comments
        checkpoint = self.job / "observations.json"
        argv = ["job", "--source", str(self.source), "--work-dir", str(self.job)]
        for complete in (True, False):
            captures[0]["capture_complete"] = complete
            runner.write_state(checkpoint, self.checkpoint)
            with self.subTest(complete=complete), patch.object(sys, "argv", argv), \
                 patch.object(runner, "convert_xlsx_sheets", return_value=rows), \
                 patch.object(runner, "run_capture", side_effect=AssertionError("already visible")), \
                 patch.object(runner, "build_workbook"), patch.object(runner, "write_manifest"):
                self.assertEqual(runner.main(), 0)
                results = json.loads((self.job / "review-report.json").read_text(encoding="utf-8"))["results"]
                self.assertEqual([item["triage"] for item in results], ["成功", "成功"])

        for change in ({"comments": comments[:1]}, {"target_key": "another"}, {"parser_revision": "old"},
                       {"captured_at": (self.now - timedelta(days=2)).isoformat()}, {"capture_status": "captcha"}):
            with self.subTest(change=change):
                checkpoint_data = {"observations": [{**captures[0], **change}]}
                self.assertFalse(runner.checkpoint_covers_workbook(rows, checkpoint_data, {"n1": rows}))

    def test_incomplete_first_note_recovers_and_reviews_every_row(self):
        observations = self.checkpoint["observations"]
        calls = []
        def collect(argv):
            key = argv[argv.index("--only-note-id") + 1] if "--only-note-id" in argv else None
            calls.append(key)
            if key is None:
                captures = observations
            else:
                captures = [{**observations[0], "capture_complete": False, "comments": []}]
                if key == "n2":
                    captures.append(observations[1])
            runner.write_state(self.job / "observations.json", {"observations": captures})
        argv = ["job", "--source", str(self.source), "--work-dir", str(self.job)]
        with patch.object(sys, "argv", argv), patch.object(runner, "run_capture", side_effect=collect):
            self.assertEqual(runner.main(), 0)
        self.assertEqual(calls, ["n1", "n2", None])
        manifest = json.loads((self.job / "job-result.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["total"], len(self.rows))

    def test_platform_stop_records_unvisited_rows_without_opening_another_note(self):
        import subprocess
        def stop(argv):
            runner.write_state(self.job / "observations.json", {"observations": [
                {"platform": "xiaohongshu", "target_key": "n1", "capture_status": "captcha"}
            ], "stopped": True, "stop_reason": "captcha"})
            raise subprocess.CalledProcessError(3, argv)
        argv = ["job", "--source", str(self.source), "--work-dir", str(self.job)]
        with patch.object(sys, "argv", argv), patch.object(runner, "run_capture", side_effect=stop) as calls:
            self.assertEqual(runner.main(), 3)
        self.assertEqual(calls.call_count, 1)
        manifest = json.loads((self.job / "job-result.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["coverage"]["rows_without_capture_record"], 5)

    def test_only_incomplete_actionable_note_is_recaptured_before_export(self):
        original = self.checkpoint["observations"]
        original[2]["capture_complete"] = False
        runner.write_state(self.job / "observations.json", self.checkpoint)
        calls = []
        def collect(argv):
            keys = [argv[i + 1] for i, value in enumerate(argv) if value == "--only-note-id"]
            calls.append(keys)
            if keys:
                self.assertEqual(keys, ["n3"])
                recovered = {**original[2], "capture_complete": True,
                             "comments": [{"text":"待验证", "has_image":False}]}
                path = Path(argv[argv.index("--output") + 1])
                runner.write_state(path, {"observations": [recovered]})
        argv = ["job", "--source", str(self.source), "--work-dir", str(self.job)]
        with patch.object(sys, "argv", argv), patch.object(runner, "run_capture", side_effect=collect):
            self.assertEqual(runner.main(), 0)
        self.assertEqual(calls, [[], ["n3"]])
        results = json.loads((self.job / "review-report.json").read_text(encoding="utf-8"))["results"]
        self.assertEqual(len(results), 6)
        self.assertEqual(results[2]["triage"], "成功")

    def test_recheck_preserves_an_observed_comment_if_the_new_view_loses_it(self):
        rows = [{**row, "target_key": "n1"} for row in self.rows[:2]]
        prior = {**self.checkpoint["observations"][0], "capture_complete": False}
        first = {"observations": [prior]}
        report = runner.compare(rows, [prior], amount_mode="expected-only")
        def empty_page(argv):
            path = Path(argv[argv.index("--output") + 1])
            runner.write_state(path, {"observations": [{**prior, "comments": []}]})
        with patch.object(runner, "run_capture", side_effect=empty_page):
            checkpoint, checked = runner.retry_incomplete_notes(
                rows, first, report, self.job / "checkpoint.json", ["capture", "--output", "initial.json"], set())
        self.assertEqual(checkpoint["observations"], [prior])
        self.assertEqual([r["triage"] for r in checked["results"]], ["成功", "待人审"])

    def test_manifest_accounts_for_visible_tasks_with_overlapping_payment_claims(self):
        rows = [self.rows[0], {**self.rows[0], "source_row": 3, "case_id": "duplicate-task"}]
        captures = self.checkpoint["observations"][:1]
        runner.write_state(self.job / "observations.json", {"observations": captures})
        argv = ["job", "--source", str(self.source), "--work-dir", str(self.job)]
        with patch.object(sys, "argv", argv), patch.object(runner, "convert_xlsx_sheets", return_value=rows), \
             patch.object(runner, "run_capture", side_effect=AssertionError("already visible")):
            self.assertEqual(runner.main(), 0)
        manifest = json.loads((self.job / "job-result.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["success"], manifest["manual_review"], manifest["settlement_pending_count"]),
                         (2, 0, 2))
        self.assertEqual(manifest["approved_amount"], 0)

    def test_completed_checkpoint_uses_source_rates_and_writes_all_outputs(self):
        with zipfile.ZipFile(self.source) as archive:
            parts = {name: archive.read(name) for name in archive.namelist()}
        name = "xl/worksheets/sheet1.xml"
        original = "金额【文字1.5，文字加图2元，单图1.5】".encode()
        self.assertIn(original, parts[name])
        parts[name] = parts[name].replace(original, "金额【文字3，文字加图5元，单图4】".encode())
        with zipfile.ZipFile(self.source, "w") as archive:
            for name, data in parts.items():
                archive.writestr(name, data)
        (self.job / "observations.json").write_text(json.dumps(self.checkpoint), encoding="utf-8")
        argv = ["job", "--source", str(self.source), "--work-dir", str(self.job)]
        with patch.object(sys, "argv", argv), patch.object(runner, "run", side_effect=AssertionError("unexpected subprocess")):
            self.assertEqual(runner.main(), 0)
        expected = json.loads((self.job / "expected.json").read_text(encoding="utf-8"))
        self.assertEqual(expected["rows"][0]["expected_amount"], "3.00")
        report = json.loads((self.job / "review-report.json").read_text(encoding="utf-8"))
        self.assertEqual(len(report["results"]), 6)
        manifest = json.loads((self.job / "job-result.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["approved_amount"], 15)
        self.assertTrue(zipfile.is_zipfile(manifest["output"]))
