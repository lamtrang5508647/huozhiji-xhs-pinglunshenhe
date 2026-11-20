import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from huozhiji_audit import audit_workbook, review
from test_portable_api import fixture

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/comment-review-audit/scripts"
sys.path.insert(0, str(SCRIPTS))
import run_comment_review_job as runner
import xhs_batch_capture as xhs
from capture_resume import PARSER_REVISIONS, recheck_scope


class SuccessRecheckRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.raw = self.root / "raw.xlsx"
        self.audited = self.root / "first.xlsx"
        self.evidence = fixture(self.raw)
        # Row 2 and row 3 are on one note. Only row 2 succeeded on the first pass.
        self.move_source_note(b"n2", b"n1")
        self.make_first_pass()
        self.job = self.root / "second"
        self.job.mkdir()

    def move_source_note(self, old, new):
        with zipfile.ZipFile(self.raw) as z:
            parts = {name:z.read(name) for name in z.namelist()}
        key = "xl/worksheets/sheet1.xml"
        parts[key] = parts[key].replace(b"/explore/" + old, b"/explore/" + new)
        with zipfile.ZipFile(self.raw, "w") as z:
            for name, contents in parts.items():
                z.writestr(name, contents)

    def make_first_pass(self):
        audit_workbook(self.raw, self.evidence, self.audited)

    def invoke(self, collector):
        argv = ["job", "--source", str(self.audited), "--work-dir", str(self.job)]
        with patch.object(sys, "argv", argv), patch.object(runner, "run_capture", side_effect=collector):
            return runner.main()

    def test_fresh_second_pass_and_new_invocation_preserve_old_evidence(self):
        previous = self.job / "observations.json"
        previous.write_text(json.dumps(self.evidence), encoding="utf-8")
        old_bytes = previous.read_bytes()
        calls = []
        def collect(argv):
            path = Path(argv[argv.index("--output") + 1])
            self.assertNotEqual(path, previous)
            rows = json.loads(Path(argv[argv.index("--expected") + 1]).read_text(encoding="utf-8"))["rows"]
            active = runner.rows_to_review(rows)
            plan = xhs.targets_from_workbooks([str(self.audited)], task_rows=active)
            self.assertEqual({n["note_id"] for n in plan}, {"n1", "n4", "n5", "n6"})
            self.assertEqual(plan[0]["sources"][0]["row"], 2)
            self.assertEqual(len(plan[0]["sources"]), 1)
            selected = [argv[argv.index("--only-note-id") + 1]] if "--only-note-id" in argv else [n["note_id"] for n in plan]
            calls.append((path, tuple(selected)))
            fresh = []
            for item in self.evidence["observations"]:
                if item["target_key"] not in selected:
                    continue
                fresh.append(dict(item, capture_complete=True, image_detection_complete=True,
                                  captured_at=datetime.now(timezone.utc).isoformat(),
                                  parser_revision=PARSER_REVISIONS["xiaohongshu"]))
                if item["target_key"] == "n1":
                    fresh[-1]["comments"] = [{"text":"找不到","has_image":False}]
            runner.write_state(path, {"observations": fresh, "recheck_scope":recheck_scope(rows)})

        for _ in range(2):
            self.assertEqual(self.invoke(collect), 0)
            report = json.loads((self.job / "review-report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["audit_pass"], {
                "mode": "success_recheck", "reviewed_rows": 4, "carried_rows": 2,
                "reviewed_triage_counts": {"成功": 3, "失败": 1, "待人审": 0},
            })
            self.assertEqual([(r["review_action"], r["triage"]) for r in report["results"][:3]],
                             [("rechecked","失败"),("carried_forward","失败"),("carried_forward","待人审")])
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[0][0], calls[1][0])
        self.assertNotEqual(calls[0][0], calls[2][0])
        self.assertTrue(calls[0][0].is_file())
        self.assertEqual(previous.read_bytes(), old_bytes)

    def test_no_successful_rows_does_not_open_browser(self):
        for item in self.evidence["observations"]:
            item.update(capture_complete=True, image_detection_complete=True, comments=[])
        self.make_first_pass()
        self.assertEqual(self.invoke(AssertionError("unexpected browser")), 0)
        result = json.loads((self.job / "job-result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["audit_pass"]["reviewed_rows"], 0)
        self.assertEqual(result["audit_pass"]["carried_rows"], 6)
        self.assertEqual(result["failure"], 6)
        self.assertTrue(Path(result["output"]).is_file())

    def test_mixed_image_tasks_agree_with_sdk_before_and_after_retry(self):
        self.move_source_note(b"n6", b"n4")
        self.make_first_pass()
        rows = runner.convert_xlsx_sheets(self.audited, {}, None, skip_summary=True)
        source_rows = [row for row in rows if row["target_key"] == "n4"]
        self.assertEqual([r["prior_review"]["triage"] for r in source_rows], ["成功", "失败"])
        captures = [dict(item, captured_at=datetime.now(timezone.utc).isoformat(),
                         parser_revision=PARSER_REVISIONS["xiaohongshu"])
                    for item in self.evidence["observations"] if item["target_key"] in {"n1", "n4", "n5"}]
        photo_note = next(item for item in captures if item["target_key"] == "n4")
        photo_note["comments"] = [{"text": "", "has_image": True}]
        expected_results = review(rows, captures)["results"]
        self.assertIn("ambiguous_image_only_candidate", expected_results[3]["reason_codes"])
        for initially_complete in (True, False):
            with self.subTest(initially_complete=initially_complete):
                calls = []
                def collect(argv):
                    dest = Path(argv[argv.index("--output") + 1])
                    keys = [argv[i + 1] for i, value in enumerate(argv) if value == "--only-note-id"]
                    calls.append(keys)
                    evidence = [dict(item) for item in captures if not keys or item["target_key"] in keys]
                    if not initially_complete and not keys:
                        next(item for item in evidence if item["target_key"] == "n4")["capture_complete"] = False
                    runner.write_state(dest, {"observations": evidence, "recheck_scope": recheck_scope(rows)})
                self.assertEqual(self.invoke(collect), 0)
                report = runner.read_checkpoint(self.job / "review-report.json")
                self.assertEqual(report["results"], expected_results)
                self.assertEqual(calls, [["n1"], []] + ([] if initially_complete else [["n4"]]))

    def test_retry_keeps_visible_combo_if_only_an_ambiguous_image_remains(self):
        original = runner.convert_xlsx_sheets(self.audited, {}, None, skip_summary=True)
        rows = [{**original[i], "target_key": "mixed", "prior_review": {"triage": label}}
                for i, label in [(3, "成功"), (0, "成功"), (5, "失败")]]
        active = runner.rows_to_review(rows)
        prior = {"target_key": "mixed", "platform": "xiaohongshu", "capture_complete": False, "capture_status": "ok",
                 "comments": [{"text": active[0]["comment_text"], "has_image": True}]}
        report = runner.compare(active, [prior], amount_mode="expected-only", reply_context_rows=rows)
        self.assertEqual([r["triage"] for r in report["results"]], ["成功", "待人审"])
        def collect(argv):
            fresh = {**prior, "capture_complete": True, "comments": [
                {"text": "", "has_image": True}, {"text": active[1]["comment_text"], "has_image": False}]}
            runner.write_state(Path(argv[argv.index("--output") + 1]), {"observations": [fresh]})
        with patch.object(runner, "run_capture", side_effect=collect):
            checkpoint, checked = runner.retry_incomplete_notes(
                active, {"observations": [prior]}, report, self.job / "retry.json",
                ["capture", "--output", "old.json"], set(), context_rows=rows)
        self.assertEqual(checkpoint["observations"], [prior])
        self.assertEqual(checked["results"], report["results"])

    def test_interrupted_run_keeps_the_same_recheck_id_and_counts_only_selected_rows(self):
        paths = []
        def gate(argv):
            path = Path(argv[argv.index("--output") + 1])
            paths.append(path)
            rows = runner.read_checkpoint(self.job / "expected.json")["rows"]
            runner.write_state(path, {"stopped":True, "stop_reason":"captcha", "recheck_scope":recheck_scope(rows),
                                       "observations":[{"platform":"xiaohongshu","target_key":"n1","capture_status":"captcha"}]})
            raise subprocess.CalledProcessError(3, argv)
        for _ in range(2):
            self.assertEqual(self.invoke(gate), 3)
            result = json.loads((self.job / "job-result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["coverage"]["task_rows"], 4)
            self.assertEqual(result["coverage"]["rows_with_capture_record"], 1)
        self.assertEqual(paths[0], paths[1])

    def test_direct_capture_rejects_an_unbound_checkpoint_without_mutating_it(self):
        rows = runner.convert_xlsx_sheets(self.audited, {}, None, skip_summary=True)
        expected = self.job / "expected.json"
        runner.write_state(expected, {"rows":rows})
        old = self.job / "evidence.json"
        runner.write_state(old, self.evidence)
        args = ["--input", str(self.audited), "--expected", str(expected), "--task-targets", "--output", str(old)]
        before = old.read_bytes()
        with patch.object(xhs, "load_xhs_modules", side_effect=AssertionError("unrequested browser")):
            with self.assertRaises(SystemExit):
                xhs.main(args)
        self.assertEqual(before, old.read_bytes())

    def test_direct_capture_resumes_only_bound_active_targets(self):
        rows = runner.convert_xlsx_sheets(self.audited, {}, None, skip_summary=True)
        expected, output = self.job / "expected.json", self.job / "fresh.json"
        runner.write_state(expected, {"rows": rows})
        active = {"n1", "n4", "n5", "n6"}
        now = datetime.now(timezone.utc).isoformat()
        seen = [dict(item, captured_at=now, parser_revision=PARSER_REVISIONS["xiaohongshu"])
                for item in self.evidence["observations"] if item["target_key"] in active]
        runner.write_state(output, {"observations": seen, "recheck_scope": recheck_scope(rows)})
        args = ["--input", str(self.audited), "--expected", str(expected), "--output", str(output), "--task-targets"]
        with patch.object(xhs, "load_xhs_modules", side_effect=AssertionError("unexpected browser")):
            for note in ("n1", "n3"):
                self.assertEqual(xhs.main(args + ["--only-note-id", note]), 0)
            self.assertEqual(xhs.main(args), 0)
            output.unlink()
            for item in self.evidence["observations"]:
                item.update(capture_complete=True, image_detection_complete=True, comments=[])
            self.make_first_pass()
            rows = runner.convert_xlsx_sheets(self.audited, {}, None, skip_summary=True)
            runner.write_state(expected, {"rows": rows})
            self.assertEqual(xhs.main(args), 0)
            self.assertEqual(runner.read_checkpoint(output), {
                "observations": [], "recheck_scope": recheck_scope(rows), "stopped": False,
            })
