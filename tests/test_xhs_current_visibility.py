"""Current visibility requires the current workbook, latest capture and displayed comments."""
import sys
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from test_portable_api import fixture
from test_standalone_xhs_recheck import source_book

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/comment-review-audit/scripts"))
import run_comment_review_job as job
import xhs_batch_capture as xhs
from capture_resume import PARSER_REVISIONS


class CurrentVisibilityTests(unittest.TestCase):
    def test_newest_complete_checkpoint_can_reverse_an_older_visible_target(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            book = folder / "tasks.xlsx"
            captures = fixture(book)["observations"]
            for c in captures:
                c.update(capture_complete=True, captured_at=datetime.now(timezone.utc).isoformat(),
                         parser_revision=PARSER_REVISIONS["xiaohongshu"])
            newer = dict(captures[0], comments=[])
            job.write_state(folder / "observations.json", {"observations": [*captures, newer]})
            with patch.object(sys, "argv", ["job","--source",str(book),"--work-dir",directory,"--no-screenshots"]), \
                 patch.object(job,"run_capture", side_effect=AssertionError("checkpoint already complete")):
                self.assertEqual(job.main(), 0)
            report = job.read_checkpoint(folder / "review-report.json")
            self.assertEqual(report["results"][0]["triage"], "失败")
            self.assertEqual(report["results"][0]["approved_amount"], "0.00")

    def test_same_filename_with_changed_review_results_or_cleared_history(self):
        with tempfile.TemporaryDirectory() as directory:
            book = Path(directory, "tasks.xlsx")
            expected = Path(directory, "old.json")
            source_book(book, ["成功", "失败", "失败"])
            old = xhs.convert_xlsx_sheets(book, {}, None, skip_summary=True)
            job.write_state(expected, {"rows": old})
            for statuses, wanted in [(["失败", "失败", "成功"], [4]), (["", "", ""], [2,3,4])]:
                with self.subTest(statuses=statuses):
                    source_book(book, statuses)
                    groups = xhs.capture_task_groups([str(book)], [str(expected)], {})
                    self.assertEqual([r["source_row"] for group in groups for r in job.rows_to_review(group)], wanted)

    def test_cached_only_body_is_not_visible_on_missing_or_partial_panel(self):
        raw = [{"id":"cached", "content":"hidden in internal state"}]
        row = dict(case_id="t", platform="xiaohongshu", target_key="note", comment_text=raw[0]["content"],
                   expected_amount="2", prior_review={"triage":"成功"})
        args = Namespace(expand_dom=True, dom_max_rounds=0, incomplete_dom_retry_rounds=0)
        for panel in (False, True):
            with self.subTest(panel=panel), patch.object(xhs,"open_comments_panel",return_value=panel), \
                 patch.object(xhs, "expand_dom_comments", return_value=([{"text":"another", "has_image":False}],False,True)):
                obs, _, _ = xhs.check_note_comments(None,"note",raw,1,[row],args)
                self.assertEqual(job.compare([row],[obs],amount_mode="expected-only")["results"][0]["triage"], "待人审")
                self.assertNotIn(raw[0]["content"], [c["text"] for c in obs["comments"]])

    def test_retry_keeps_displayed_rows_from_both_partial_views_only(self):
        raw = [{"id":"cached", "content":"never displayed"}]
        args = Namespace(expand_dom=True, dom_max_rounds=0, incomplete_dom_retry_rounds=1)
        views = [([{"text":s, "has_image":False, "comment_id":s}],False,True) for s in ("earlier","later")]
        with patch.object(xhs,"open_comments_panel",return_value=True), patch.object(xhs,"expand_dom_comments",side_effect=views):
            obs, _, _ = xhs.check_note_comments(None,"note",raw,3,[],args)
            self.assertEqual({c["text"] for c in obs["comments"]}, {"earlier", "later"})

    def test_link_only_mode_can_read_visible_state_without_clicking(self):
        args = Namespace(expand_dom=False, dom_max_rounds=30, incomplete_dom_retry_rounds=20)
        with patch.object(xhs,"open_comments_panel",return_value=True), \
             patch.object(xhs,"expand_dom_comments",return_value=([{"text":"shown","has_image":False}],False,True)) as read:
            obs, _, _ = xhs.check_note_comments(None,"n",[{"id":"old","content":"cached"}],None,[],args)
            self.assertEqual(read.call_args.args[1], 0)
            self.assertEqual(read.call_count,1)
            self.assertEqual([c["text"] for c in obs["comments"]], ["shown"])

    def test_v7_offline_comparison_keeps_provably_displayed_rows(self):
        req = dict(case_id="1", platform="xiaohongshu", target_key="n", comment_text="target", expected_amount="1")
        card = {"comment_id":"1","text":"target","has_image":False}
        base = dict(platform="xiaohongshu",target_key="n",capture_status="ok",parser_revision="xhs-visible-controls-v7",
                    capture_complete=False,comments=[card])
        for change, verdict in [
            ({}, "待人审"),
            ({"visible_comment_panel_readable":True}, "待人审"),
            ({"capture_stop_reason":"recheck_matches_visible"}, "成功"),
            ({"comments":[dict(card,text_source="dom_note_text")]}, "成功"),
            ({"capture_complete":True,"capture_complete_source":"visible_comment_list_end"}, "成功"),
            ({"capture_complete":True,"capture_complete_source":"visible_comment_list_end","comments":[]}, "失败"),
        ]:
            with self.subTest(change=change):
                result = job.compare([req], [dict(base,**change)], amount_mode="expected-only")["results"][0]
                self.assertEqual(result["triage"], verdict)

    def test_run_does_not_issue_a_completion_for_an_unvisited_platform(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            book = folder / "tasks.xlsx"
            fixture(book)
            rows = job.convert_xlsx_sheets(book, {}, None, skip_summary=True)
            rows[-1]["platform"] = "douyin"
            with patch.object(sys, "argv", ["job","--source",str(book),"--work-dir",directory]), \
                 patch.object(job,"convert_xlsx_sheets",return_value=rows), \
                 patch.object(job,"run_capture",side_effect=AssertionError("mixed input should not begin a session")):
                self.assertEqual(job.main(), 1)
            state = job.read_checkpoint(folder / "job-result.json")
            self.assertEqual(state["status"], "error")
            self.assertIn("capture-douyin", state["reason"])
