import unittest

from huozhiji_audit import review


def task(case_id, text, **fields):
    return dict(platform="xiaohongshu", target_key="note", source_sheet="Sheet",
                case_id=case_id, comment_text=text, expected_amount="2.00", **fields)


def comment(comment_id, text, **fields):
    return dict(comment_id=comment_id, text=text, has_image=False, **fields)


def audit(rows, comments):
    capture = dict(platform="xiaohongshu", target_key="note", capture_complete=True,
                   capture_status="ok", image_detection_complete=True, comments=comments)
    return {r["case_id"]: r for r in review(rows, [capture], require_reply_parent=True)["results"]}


class ReplyAttributionTests(unittest.TestCase):
    def setUp(self):
        self.root = task("r", "root text", source_row=3)
        self.reply = task("a", "reply text", source_row=4, reply_to="回复第1条")
        self.parent = comment("root", "root text", parent_comment_id=None)

    def test_wrong_thread_unknown_and_missing_are_manual(self):
        for matches in ([], [comment("c", "reply text")],
                        [comment("c", "reply text", parent_comment_id=None)],
                        [comment("c", "reply text", parent_comment_id="some-other-root")],
                        [comment("c", "reply text", parent_comment_id="root", parent_text="unrelated")]):
            with self.subTest(matches=matches):
                row = audit([self.root, self.reply], [self.parent, *matches])["a"]
                self.assertEqual(row["triage"], "待人审")
                self.assertIsNone(row["approved_amount"])
                self.assertIn("reply_parent_unconfirmed", row["reason_codes"])

    def test_direct_parent_filters_duplicate_text_and_retains_evidence(self):
        for relation in ({"parent_comment_id": "root"}, {"parent_text": "Root text"}):
            comments = [self.parent, comment("wrong", "reply text", parent_comment_id=None),
                        comment("right", "reply text", **relation)]
            row = audit([self.reply, self.root], comments)["a"]
            self.assertEqual(row["approved_amount"], "2.00")
            self.assertEqual(row["matched_comment"]["comment_id"], "right")
            for field in relation:
                self.assertIn(field, row["matched_comment"])

    def test_single_visible_reply_retains_page_basis_and_checks_its_prerequisites(self):
        basis = "single_visible_reply_under_root"
        displayed = comment("c", "reply text", parent_text="root text", parent_evidence=basis)
        rows = [self.root, self.reply]
        matched = audit(rows, [self.parent, displayed])["a"]["matched_comment"]
        self.assertEqual((matched["parent_evidence"], matched["parent_text"]), (basis, "root text"))
        self.assertNotIn("parent_comment_id", matched)
        for candidate in (dict(displayed, parent_text=""), dict(displayed, parent_text="other root"),
                          dict(displayed, thread_root_deleted=True)):
            row = audit(rows, [self.parent, candidate])["a"]
            self.assertEqual(row["triage"], "待人审")
            self.assertEqual(row["candidates"][0]["parent_evidence"], basis)
        base = dict(platform="xiaohongshu", target_key="note", capture_complete=False, capture_status="ok")
        partial = dict(base, comments=[self.parent, displayed])
        row = review(rows, [partial], require_reply_parent=True)["results"][1]
        self.assertIn("reply_parent_unconfirmed", row["reason_codes"])
        direct = dict(base, comments=[self.parent, comment("c", "reply text", parent_comment_id="root")])
        row = review(rows, [direct], require_reply_parent=True)["results"][1]
        self.assertEqual(row["status"], "verified")
        self.assertNotIn("parent_evidence", row["matched_comment"])
        earlier = dict(base, capture_complete=True, comments=[
            dict(self.parent), comment("c", "reply text", parent_text="root text")])
        later = dict(base, capture_complete=True, comments=[dict(self.parent), displayed])
        row = review(rows, [earlier, later], require_reply_parent=True)["results"][1]
        self.assertEqual(row["matched_comment"]["parent_evidence"], basis)

    def test_missing_or_ambiguous_parent_is_manual(self):
        matched = comment("c", "reply text", parent_comment_id="root")
        competing = comment("second", "root text")
        for rows, comments in (([self.reply], [self.parent, matched]),
                               ([self.root, self.reply], [self.parent, competing, matched]),
                               ([self.root, self.reply, {**self.root, "case_id":"second", "source_row":5}],
                                [self.parent, matched]),
                               ([{**self.root, "source_sheet":"Other"}, self.reply], [self.parent, matched]),
                               ([{**self.root, "target_key":"other"}, self.reply], [self.parent, matched])):
            self.assertEqual(audit(rows, comments)["a"]["triage"], "待人审")

    def test_two_snapshots_cannot_manufacture_a_parent_link(self):
        base = dict(platform="xiaohongshu", target_key="note", capture_status="ok", capture_complete=True)
        pair = [dict(base, comments=[self.parent]),
                dict(base, comments=[comment("root", "different body"), comment("c", "reply text", parent_comment_id="root")])]
        r = review([self.root, self.reply], pair, require_reply_parent=True)["results"][1]
        self.assertIn("reply_parent_unconfirmed", r["reason_codes"])

    def test_referenced_parent_level_and_identifier_must_be_consistent(self):
        child = comment("c", "reply text", parent_comment_id="root", parent_text="root text")
        for parent in (dict(self.parent, parent_comment_id="grandparent"),
                       dict(self.parent, parent_text="grandparent text"),
                       dict(self.parent, comment_id=None)):
            with self.subTest(parent=parent):
                result = audit([self.root, self.reply], [parent, child])
                self.assertIn("reply_parent_unconfirmed", result["a"]["reason_codes"])
                self.assertIsNone(result["a"]["approved_amount"])
                # The independent row asks for text only and carries no placement constraint.
                self.assertEqual(result["r"]["status"], "verified")
        nested = dict(self.parent, parent_comment_id="upper", comment_id="other")
        rows = [self.root, self.reply]
        self.assertEqual(audit(rows, [self.parent, nested, child])["a"]["status"], "verified")
        conflict = dict(nested, comment_id=self.parent["comment_id"])
        self.assertEqual(audit(rows, [self.parent, conflict, child])["a"]["triage"], "待人审")

    def test_ordinal_conflict_and_non_directive(self):
        third = task("b", "another root", source_row=5)
        fourth = task("c", "another reply", source_row=6, reply_to="回复第2条")
        rows = [self.root, self.reply, third, fourth]
        comments = [self.parent, comment("second", "another root"), comment("c", "another reply", parent_comment_id="second")]
        self.assertEqual(audit(rows, comments)["c"]["triage"], "待人审")
        for ordinary in ("", "optional source note"):
            row = audit([{**self.reply, "reply_to":ordinary}], [comment("c", "reply text")])["a"]
            self.assertEqual(row["triage"], "成功")

    def test_all_row_number_is_unique_when_root_only_number_is_out_of_range(self):
        third = task("b", "third task", source_row=5)
        fourth = task("c", "final reply", source_row=6, reply_to="回复第3条")
        observed = [self.parent, comment("third", "third task", parent_comment_id=None),
                    comment("fourth", "final reply", parent_comment_id="third")]
        self.assertEqual(audit([self.root, self.reply, third, fourth], observed)["c"]["status"], "verified")
        # An out-of-range root count cannot certify a future parent, self-reference or a reply.
        after = task("f", "future root", source_row=6)
        ask_future = task("d", "asks fourth", source_row=5, reply_to="回复第4条")
        for rows in ([self.root, self.reply, ask_future, after],
                     [self.root, self.reply, {**fourth, "source_row":5}],
                     [self.root, self.reply, {**fourth, "source_row":5, "reply_to":"回复第2条"}]):
            row = next(r for r in audit(rows, observed).values() if r["case_id"] in ("c", "d"))
            self.assertIn("reply_source_parent_ambiguous", row["reason_codes"])

    def test_manual_reply_carries_target_recheck_record(self):
        capture = dict(platform="xiaohongshu", target_key="note", capture_status="ok", capture_complete=True,
                       comments=[self.parent, comment("top", "reply text", parent_comment_id=None)])
        for source, attempted, panel in (({}, None, None),
                                         ({"page_attempted": False}, False, None),
                                         ({"page_attempted": True, "panel_available": False}, True, False),
                                         ({"page_attempted": True, "panel_available": True}, True, True)):
            marked = dict(capture, reply_verification=source) if source else capture
            results = review([self.root, self.reply], [marked], require_reply_parent=True)["results"]
            self.assertNotIn("reply_verification", results[0])
            check = results[1]["reply_verification"]
            self.assertEqual(check["page_attempted"], attempted)
            self.assertEqual(check["panel_available"], panel)
            self.assertEqual(check["scope"], "target")
            self.assertEqual(check["reason"], "reply_parent_unconfirmed")
            self.assertEqual(results[1]["candidates"][0]["comment_id"], "top")
        r = review([self.reply], [capture], require_reply_parent=True)["results"][0]
        self.assertIn("reply_source_parent_ambiguous", r["reason_codes"])
        self.assertIsNone(r["approved_amount"])
        attempted = dict(capture, captured_at="2026-01-01T12:00:00Z", reply_verification={
            "page_attempted": True, "panel_available": True, "attempt_count": 1,
            "unconfirmed_before": 1, "unconfirmed_after": 1, "status": "checked_unresolved"})
        check = review([self.root, self.reply], [attempted, capture], require_reply_parent=True)["results"][1]["reply_verification"]
        self.assertEqual((check["captured_at"], check["attempt_count"], check["unconfirmed_after"], check["status"]),
                         ("2026-01-01T12:00:00Z", 1, 1, "checked_unresolved"))

    def test_image_match_cannot_leak_across_threads(self):
        image = {**self.reply, "comment_type":"image", "comment_text":"", "has_image":True}
        wrong = comment("pic", "", parent_comment_id=None)
        wrong["has_image"] = True
        other = comment("txt", "another reply", parent_comment_id="root")
        row = audit([self.root, image], [self.parent, wrong, other])["a"]
        self.assertNotEqual(row["triage"], "成功")
        good = dict(wrong, comment_id="right", parent_comment_id="root")
        row = audit([self.root, image], [self.parent, wrong, other, good])["a"]
        self.assertEqual(row["matched_comment"]["comment_id"], "right")
        combo = {**self.reply, "comment_type":"text+image", "partial_text_amount":"1.00"}
        row = audit([self.root, combo], [self.parent, comment("c", "reply text", parent_comment_id="root")])["a"]
        self.assertEqual((row["status"], row["approved_amount"]), ("partial_text", "1.00"))

    def test_deleted_root_is_a_fact_about_a_matching_candidate_only(self):
        removed = comment("c1", "reply text", thread_root_deleted=True)
        other = comment("c2", "reply text")
        unrelated = comment("c3", "unrelated", thread_root_deleted=True)
        row = audit([self.root, self.reply], [self.parent, removed, other, unrelated])["a"]
        self.assertEqual((row["triage"], row["approved_amount"]), ("待人审", None))
        self.assertTrue(row["candidates"][0]["thread_root_deleted"])
        self.assertEqual(row["reason_codes"], ["reply_parent_unconfirmed", "reply_thread_root_deleted"])
        self.assertEqual(row["reply_verification"]["matching_candidates_with_deleted_root"], 1)
        self.assertEqual(row["reply_verification"]["reason"], "reply_parent_unconfirmed")
        self.assertEqual(row["reply_verification"]["reason_codes"], row["reason_codes"])
        unknown = audit([self.root, self.reply], [self.parent, unrelated, other])["a"]
        self.assertNotIn("reply_thread_root_deleted", unknown["reason_codes"])
        # The source is itself ambiguous even if there are also no platform observations.
        missing = review([self.reply], [], require_reply_parent=True)["results"][0]
        self.assertIn("no_observation_for_target", missing["reason_codes"])
        self.assertEqual(missing["reply_verification"]["reason"], "reply_source_parent_ambiguous")
