import base64
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

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

    def test_refresh_keeps_cross_platform_and_unvisited_capture_history(self):
        douyin = {"target_key":"same","platform":"douyin"}
        xhs = {"target_key":"same","platform":"xiaohongshu"}
        older = dict(douyin, target_key="other", captured_at="earlier")
        later = dict(older, captured_at="later")
        fresh = dict(douyin, captured_at="fresh")
        original = [xhs, douyin, older, later]
        previous = {"same":douyin, "other":later}
        self.assertEqual(dy.saved_observations(original, previous, {**previous,"same":fresh}),
                         [xhs, older, later, fresh])

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

    def test_default_capture_requires_verified_existing_image_to_skip_a_visit(self):
        self.write([("123","成功")])
        _, scope = dy.capture_plan([self.source])
        now = datetime.now(timezone.utc).isoformat()
        old = dict(platform="douyin", target_key="123", capture_status="ok", capture_complete=True,
                   parser_revision=dy.PARSER_REVISIONS["douyin"], captured_at=now,
                   captured_comment_count=0, comments=[], image_detection_complete=True)
        args=["capture","--expected",str(self.source),"--output",str(self.output),"--profile",str(self.output.parent/"profile")]
        def checkpoint():
            self.output.write_text(json.dumps(dict(recheck_scope=scope,observations=[old])),encoding="utf8")
        checkpoint()
        with patch.object(sys,"argv",args+["--no-screenshots"]), patch.object(dy,"sync_playwright",side_effect=AssertionError("unexpected browser")):
            self.assertEqual(dy.main(),0)
        with patch.object(sys,"argv",args), patch.object(dy,"network_fingerprint",return_value="same"), patch.object(
                dy,"sync_playwright",side_effect=RuntimeError("recapture required")):
            with self.assertRaisesRegex(RuntimeError,"recapture required"):
                dy.main()
            writer=dy.NoteScreenshots(self.output.parent/"评论截图","douyin","123")
            png=base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+d7ckAAAAASUVORK5CYII=")
            record=writer.save(Mock(screenshot=Mock(return_value=png)), "empty")
            old.update(screenshot_status="captured",comment_screenshots=writer.records)
            checkpoint()
            self.assertEqual(dy.main(),0)
            image=self.output.parent/record["path"]
            image.write_bytes(b"changed")
            with self.assertRaisesRegex(RuntimeError,"recapture required"):
                dy.main()
