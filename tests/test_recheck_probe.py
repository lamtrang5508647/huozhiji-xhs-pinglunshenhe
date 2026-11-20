import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/comment-review-audit/scripts"))
import run_comment_review_job as runner
import xhs_batch_capture as xhs
from capture_resume import PARSER_REVISIONS


def comment(text):
    return {"text": text, "has_image": False, "comment_id": "displayed"}


def probe(key):
    return {"platform": "xiaohongshu", "target_key": key, "resolved_target_key": key,
            "parser_revision": PARSER_REVISIONS["xiaohongshu"], "captured_at": datetime.now(timezone.utc).isoformat(),
            "capture_status": "ok", "capture_complete": False, "comments": [comment("other comment")],
            "visible_comment_panel_readable": True}


class RecheckProbeTests(unittest.TestCase):
    def test_incomplete_but_readable_first_note_allows_batch_then_one_targeted_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "input.xlsx"
            source.write_bytes(b"mock input")
            checkpoint = folder / "observations.json"
            rows = [dict(platform="xiaohongshu",target_key=key,case_id=key,comment_text="wanted",expected_amount="1",
                         target_url=f"https://www.xiaohongshu.com/explore/{key}", prior_review={"triage":"成功"})
                    for key in ("a","b","c")]
            visits = []
            def collect(argv):
                output = Path(argv[argv.index("--output") + 1])
                only = [argv[i+1] for i,opt in enumerate(argv) if opt == "--only-note-id"]
                skip = [argv[i+1] for i,opt in enumerate(argv) if opt == "--skip-note-id"]
                keys = [key for key in (only or ["a","b","c"]) if key not in skip]
                visits.append(keys)
                records = {v["target_key"]:v for v in runner.read_checkpoint(output)["observations"]}
                for key in keys:
                    records[key] = probe(key)
                    if key == "c":
                        records[key]["comments"] = [comment("wanted")]
                runner.write_state(output, {"observations":list(records.values()), "stopped":False})
            args = SimpleNamespace(maps=[],profile="stub",normal_pace=True,wait_for_confirmation=False,capture_idle_timeout=180)
            with patch.object(runner, "run_capture", side_effect=collect):
                report = runner.collect_rows(args, source, folder/"expected.json", checkpoint, folder/"job.json",
                                             rows, {"recheck_id":"testing"}, context_rows=rows)
            self.assertEqual(visits, [["a"],["b","c"],["a","b"]])
            self.assertEqual([r["triage"] for r in report["results"]], ["待人审","待人审","成功"])

    def test_core_rejects_stale_blank_misdirected_and_challenge_probes(self):
        start = datetime.now(timezone.utc)
        item = probe("a")
        rows = [dict(target_key="a", target_url="https://www.xiaohongshu.com/explore/a")]
        for update in ({"captured_at":(start-timedelta(seconds=1)).isoformat()},
                       {"captured_at":(start+timedelta(hours=1)).isoformat()},
                       {"visible_comment_panel_readable": False}, {"resolved_target_key":"b"},
                       {"target_key":"b"}, {"comments":[]}, {"capture_status":"captcha"},
                       {"comments":[dict(comment("tombstone"),deleted=True)]}):
            with self.subTest(update=update):
                self.assertFalse(runner.readable_recheck_probe({**item,**update},"a",rows,start))
        self.assertTrue(runner.readable_recheck_probe(item,"a",rows,start))
        short_rows = [{"target_url": "https://xhslink.com/a"}]
        self.assertTrue(runner.readable_recheck_probe({**item, "resolved_target_key":"canonical"}, "a", short_rows, start))

    def test_skipped_probe_does_not_start_a_browser_when_it_is_the_only_note(self):
        with tempfile.TemporaryDirectory() as folder:
            args = ["--input", str(Path(folder,"source.xlsx")), "--output", str(Path(folder,"capture.json")),
                    "--skip-note-id", "a"]
            with patch.object(xhs,"capture_task_groups",return_value=[]), \
                 patch.object(xhs,"targets_from_workbooks",return_value=[{"note_id":"a"}]), \
                 patch.object(xhs,"load_xhs_modules",side_effect=AssertionError("unexpected browser")):
                self.assertEqual(xhs.main(args), 0)

    def test_structured_comments_alone_do_not_certify_the_visible_panel(self):
        args = SimpleNamespace(require_reply_parent=False, expand_dom=True, dom_max_rounds=1, incomplete_dom_retry_rounds=0)
        for visible, healthy in (([],False),([dict(comment("gone"),deleted=True)],False),([comment("visible")],True)):
            with self.subTest(visible=visible), patch.object(xhs,"open_comments_panel",return_value=True), \
                 patch.object(xhs,"expand_dom_comments",return_value=(visible,False,True)):
                evidence,*_ = xhs.check_note_comments(None,"a",[{"id":"s","content":"structured comment"}],2,[],args)
                self.assertEqual(bool(evidence.get("visible_comment_panel_readable")), healthy)
