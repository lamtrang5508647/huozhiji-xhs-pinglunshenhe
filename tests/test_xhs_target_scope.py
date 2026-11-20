import json
import tempfile
import unittest
from argparse import Namespace
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from test_recheck_probe import probe, runner, xhs
from test_standalone_xhs_recheck import source_book


class FetchError(Exception):
    pass


class LoginError(Exception):
    pass


def unavailable(key, state):
    return {**probe(key), "capture_status": "inaccessible", "comments": [],
            "image_detection_complete": False, "visible_comment_panel_readable": False,
            "warnings": ["comments_panel_unavailable"], "target_page_state": state}


class TargetScopeTests(unittest.TestCase):
    def test_explicit_empty_proof_is_reusable_and_inconsistent_evidence_is_not(self):
        row = dict(case_id="s", target_key="a", platform="xiaohongshu", comment_text="wanted", expected_amount="1")
        proof = {**probe("a"), "capture_complete":True, "image_detection_complete":True, "comments":[],
                 "target_page_state":"target_note", "capture_complete_source":"visible_comment_empty_state",
                 "empty_state_label":"barren_land_click_to_comment", "captured_comment_count":0,
                 "declared_comment_count":2, "warnings":["visible_terminal_with_stale_declared_count"]}
        for update in ({}, {"empty_state_label":"unrecognized"}, {"visible_comment_panel_readable":False},
                       {"captured_comment_count":2}, {"target_page_state":"unknown"}, {"capture_complete":False},
                       {"comments":[{"text":"wanted", "has_image":False}]}):
            with self.subTest(update=update):
                evidence = {**proof, **update}
                self.assertEqual(xhs.capture_covers_tasks(evidence,"a",[row]), not bool(update))
                result = runner.compare([row], [evidence], amount_mode="expected-only")["results"][0]
                self.assertEqual(result["triage"], "待人审" if update else "失败")

    def test_batch_reaches_third_note_after_two_unreadable_notes_but_stops_on_navigation_or_login(self):
        for problems, expected_code, reason, count in [
            (["target_note", "post_unavailable"], 0, "", 3),
            ([FetchError(), FetchError()], 3, "repeated_navigation_failure", 2),
            ([LoginError("captcha")], 3, "captcha", 1),
        ]:
            with self.subTest(problems=problems), tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
                source, output = Path(temp, "tasks.xlsx"), Path(temp, "observations.json")
                source_book(source, ["成功"] * 6)
                client = Mock()
                for name, value in dict(load_xhs_modules=(str, str, object, FetchError, LoginError),
                                        persistent_client=client, login_gate_visible=False, network_fingerprint="mock",
                                        paced_sleep=None, verification_status="captcha").items():
                    stack.enter_context(patch.object(xhs, name, return_value=value))
                def visit(client, key, *args, **kwargs):
                    i = int(key[-1])
                    if i < len(problems):
                        if isinstance(problems[i], Exception):
                            raise problems[i]
                        return unavailable(key, problems[i]), False, False, None
                    return {**probe(key), "capture_complete": True}, True, False, 1
                stack.enter_context(patch.object(xhs, "collect_note_evidence", side_effect=visit))
                self.assertEqual(xhs.main(["--input",str(source),"--output",str(output)]), expected_code)
                checkpoint = json.loads(output.read_text("utf-8"))
                self.assertEqual(checkpoint["stop_reason"], reason)
                self.assertEqual(len(checkpoint["observations"]), count)

    def test_canary_does_not_block_other_notes_or_retry_first_note_twice(self):
        for pass_state in ({}, {"recheck_id": "current"}):
            with self.subTest(pass_state=pass_state), tempfile.TemporaryDirectory() as temp:
                p = Path(temp)
                source, checkpoint = p/"source.xlsx", p/"observations.json"
                source.write_bytes(b"fixture")
                rows = [dict(case_id=k, platform="xiaohongshu", target_key=k, comment_text="wanted",
                             expected_amount="1", target_url=f"https://www.xiaohongshu.com/explore/{k}")
                        for k in ["a", "b", "c"]]
                visits = []
                def batch(argv):
                    output = Path(argv[argv.index("--output")+1])
                    only = [argv[i+1] for i,v in enumerate(argv) if v=="--only-note-id"]
                    skip = [argv[i+1] for i,v in enumerate(argv) if v=="--skip-note-id"]
                    keys = [k for k in (only or ["a","b","c"]) if k not in skip]
                    visits.append(keys)
                    records = {v["target_key"]:v for v in runner.read_checkpoint(output)["observations"]}
                    for k in keys:
                        if k in ("a","b"):
                            records[k] = unavailable(k, "post_unavailable" if k=="a" else "target_note")
                        else:
                            records[k] = {**probe(k), "capture_complete":True, "comments":[{"text":"wanted","has_image":False}]}
                    runner.write_state(output, {"observations":list(records.values()), "stopped":False})
                args = Namespace(profile="stub", maps=[], normal_pace=True, wait_for_confirmation=False, capture_idle_timeout=60)
                with patch.object(runner, "run_capture", side_effect=batch):
                    report = runner.collect_rows(args, source, p/"expected.json", checkpoint, p/"job.json", rows, pass_state)
                self.assertIsNotNone(report)
                self.assertEqual(visits, [["a"],["b","c"],["a","b"]])
                self.assertEqual([r["triage"] for r in report["results"]], ["待人审","待人审","成功"])

    def test_wrong_detail_or_page_entity_cannot_supply_visible_evidence(self):
        args = Namespace(expand_dom=True, dom_max_rounds=0, incomplete_dom_retry_rounds=0)
        for detail, page_states, should_fail in [
            ({"note": {"noteId":"foreign"}}, ["target_note"], True),
            ({"note": {}}, ["wrong_note"], True),
            ({"note": {"noteId":"requested"}}, ["target_note", "wrong_note"], True),
            (FetchError("ordinary data unavailable"), ["unknown"], False),
        ]:
            with self.subTest(detail=detail, page_states=page_states):
                client = Mock()
                client._page.url = "https://www.xiaohongshu.com/explore/requested"
                if isinstance(detail, Exception):
                    client.get_note_detail.side_effect = detail
                else:
                    client.get_note_detail.return_value = detail
                client.get_note_comments.return_value = []
                client._page.evaluate.side_effect = page_states * 3
                visible = {"capture_status":"ok", "comments":[{"text":"shown"}]}
                with patch.object(xhs,"login_gate_visible",return_value=False), patch.object(xhs,"reply_page_blocker",return_value=""), \
                     patch.object(xhs,"check_note_comments",return_value=(visible,True,False)):
                    if should_fail:
                        with self.assertRaisesRegex(FetchError,"note_identity"):
                            xhs.collect_note_evidence(client,"requested","requested","",[],args,LoginError,FetchError)
                    else:
                        result = xhs.collect_note_evidence(client,"requested","requested","",[],args,LoginError,FetchError)
                        self.assertEqual(result[0]["comments"], [{"text":"shown"}])

    def test_post_restriction_discards_underlying_comment_and_captcha_takes_precedence(self):
        args = Namespace(expand_dom=True, dom_max_rounds=0, incomplete_dom_retry_rounds=0)
        client = Mock()
        client._page.url = "https://www.xiaohongshu.com/explore/requested"
        client.get_note_detail.return_value = {"note":{"noteId":"requested"}}
        client.get_note_comments.return_value = []
        for states in (["post_unavailable"], ["target_note","post_unavailable"]):
            with self.subTest(states=states), patch.object(xhs, "reply_page_blocker", return_value=""), \
                 patch.object(xhs,"check_note_comments",return_value=({"capture_status":"ok","comments":[{"text":"background"}]},True,False)):
                client._page.evaluate.side_effect = states
                obs, *_ = xhs.collect_note_evidence(client,"requested","requested","",[],args,LoginError,FetchError)
                self.assertEqual((obs["capture_status"], obs["target_page_state"], obs["comments"]),
                                 ("inaccessible", "post_unavailable", []))
        client._page.url = "https://www.xiaohongshu.com/verification"
        with patch.object(xhs,"reply_page_blocker",return_value="captcha"):
            with self.assertRaisesRegex(LoginError, "captcha"):
                xhs.collect_note_evidence(client,"requested","requested","",[],args,LoginError,FetchError)
