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
        self.now = datetime.now(timezone.utc)
        for item in self.checkpoint["observations"]:
            item.update(captured_at=self.now.isoformat(), parser_revision=PARSER_REVISIONS["xiaohongshu"],
                        capture_complete=True)

    def test_every_workbook_target_needs_a_healthy_latest_capture(self):
        self.assertTrue(runner.checkpoint_covers_workbook(self.source, self.checkpoint, {}))
        self.assertFalse(runner.checkpoint_covers_workbook(self.source, {**self.checkpoint, "stopped": True}, {}))
        captures = self.checkpoint["observations"]
        self.assertFalse(runner.checkpoint_covers_workbook(self.source, {"observations": captures[:-1]}, {}))
        for change in ({"image_detection_complete": False}, {"parser_revision": "old"},
                       {"capture_status": "captcha"}, {"capture_complete": False},
                       {"captured_at": (self.now - timedelta(days=2)).isoformat()}):
            with self.subTest(change=change):
                latest = [*captures, {**captures[0], **change}]
                self.assertFalse(runner.has_healthy_capture(latest, captures[0]["target_key"]))
                self.assertFalse(runner.checkpoint_covers_workbook(self.source, {"observations": latest}, {}))

    def test_live_job_rechecks_missing_parent_even_on_a_complete_checkpoint(self):
        captures = self.checkpoint["observations"]
        root = dict(platform="xiaohongshu", target_key="n1", source_sheet="S", source_row=2,
                    case_id="root", comment_text="parent", expected_amount="1.00")
        reply = dict(root, source_row=3, case_id="reply", comment_text="child", reply_to="回复第1条")
        rows = [root, reply]
        comments = [dict(text="parent", comment_id="r", parent_comment_id=None, has_image=False),
                    dict(text="child", comment_id="c", has_image=False)]
        captures[0]["comments"] = comments
        requirements = runner.group_reply_requirements(rows)
        self.assertFalse(runner.checkpoint_covers_workbook(self.source, self.checkpoint, requirements))
        checkpoint = self.job / "observations.json"
        runner.write_state(checkpoint, self.checkpoint)

        def simulate_page_recheck(args):
            self.assertIn("--only-note-id", args)
            source_requirements = Path(args[args.index("--verify-replies") + 1])
            self.assertEqual(json.loads(source_requirements.read_text(encoding="utf-8"))["rows"], rows)
            comments[1]["parent_comment_id"] = "r"
            runner.write_state(checkpoint, self.checkpoint)

        argv = ["job", "--source", str(self.source), "--work-dir", str(self.job)]
        with patch.object(sys, "argv", argv), patch.object(runner, "convert_xlsx_sheets", return_value=rows), \
             patch.object(runner, "run_capture", side_effect=simulate_page_recheck) as capture, \
             patch.object(runner, "build_workbook"), patch.object(runner, "write_manifest"):
            self.assertEqual(runner.main(), 0)
            capture.assert_called_once()
        results = json.loads((self.job / "review-report.json").read_text(encoding="utf-8"))["results"]
        self.assertEqual(results[1]["triage"], "成功")
        self.assertTrue(runner.checkpoint_covers_workbook(self.source, self.checkpoint, requirements))

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
