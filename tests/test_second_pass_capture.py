import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/comment-review-audit/scripts"
sys.path.insert(0, str(SCRIPTS))
import douyin_batch_capture as dy


class DouyinSecondPassTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.source = Path(folder.name) / "tasks.json"
        self.output = Path(folder.name) / "capture.json"

    def write(self, pairs):
        rows = []
        for i, (key, verdict) in enumerate(pairs, 2):
            rows.append(dict(platform="douyin", target_key=key, target_url=f"https://www.douyin.com/video/{key}",
                             source_file="input.xlsx", source_sheet="Tasks", source_row=i, case_id=str(i),
                             prior_review={"category":verdict, "raw_category":verdict, "triage":verdict}))
        self.source.write_text(json.dumps({"rows":rows}), encoding="utf-8")

    def test_shared_target_only_includes_success_refs(self):
        self.write([("a","失败"), ("a","成功"), ("b","失败"), ("c","待人审")])
        targets, scope = dy.capture_plan([self.source])
        self.assertTrue(scope)
        self.assertEqual([t["target_key"] for t in targets], ["a"])
        self.assertEqual([r["row"] for r in targets[0]["source_refs"]], [3])

    def test_empty_recheck_creates_empty_evidence_without_browser_and_protects_first_pass(self):
        self.write([("a","失败")])
        args = ["capture", "--expected", str(self.source), "--output", str(self.output)]
        with patch.object(sys, "argv", args), patch.object(dy,"sync_playwright",side_effect=AssertionError("unexpected capture")):
            self.assertEqual(dy.main(), 0)
            self.assertEqual(json.loads(self.output.read_text(encoding="utf-8"))["observations"], [])
            original = b'{"observations":[],"stopped":false}'
            self.output.write_bytes(original)
            with self.assertRaises(SystemExit):
                dy.main()
            self.assertEqual(self.output.read_bytes(), original)
