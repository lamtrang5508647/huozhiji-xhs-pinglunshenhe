import unittest

from huozhiji_audit._engine.review_comments import compare


class NoteVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.task = dict(platform="xiaohongshu", target_key="note", case_id="a",
                         comment_text="目标正文", expected_amount="2", reply_to="回复第99条")
        self.card = dict(text="目标正文", comment_id="example", has_image=False)

    def capture(self, cards, **fields):
        return dict(dict(platform="xiaohongshu", target_key="note", capture_status="ok",
                         capture_complete=True, comments=cards), **fields)

    def review(self, cards, tasks=None):
        return compare(tasks or [self.task], [self.capture(cards)], amount_mode="expected-only")

    def test_any_live_card_in_the_same_note_is_enough(self):
        for fields in ({}, {"parent_comment_id": None}, {"parent_comment_id":"another"},
                       {"thread_root_deleted": True}, {"has_image":True}):
            with self.subTest(fields=fields):
                card = dict(self.card, **fields)
                report = compare([self.task], [self.capture([card], capture_complete=False)],
                                 amount_mode="expected-only")
                row = report["results"][0]
                self.assertEqual((row["triage"], row["approved_amount"]), ("成功", "2.00"))
                self.assertEqual(row["matched_comment"]["text"], self.task["comment_text"])
                self.assertNotIn("reply_verification", row)
        self.assertEqual(self.review([self.card, dict(self.card, comment_id="second")])["results"][0]["triage"], "成功")

    def test_filter_note_platform_deleted_card_and_other_text(self):
        here = self.capture([dict(self.card, deleted=True), dict(self.card, text="不同的正文"),
                             dict(self.card, text="回复 @昵称：另一条目标正文")])
        others = [self.capture([self.card], target_key="other"), self.capture([self.card], platform="douyin")]
        for complete, verdict in [(True,"失败"), (False,"待人审")]:
            with self.subTest(complete=complete):
                data = [dict(here, capture_complete=complete), *others]
                r = compare([self.task], data, amount_mode="expected-only")["results"][0]
                self.assertEqual(r["triage"], verdict)

    def test_possible_interface_label_in_one_text_node_cannot_prove_presence_or_absence(self):
        for text in ("回复 @某人 目标正文", "回复 @某人：目标正文", "回复某人：目标正文", "@某人：目标正文"):
            with self.subTest(text=text):
                combined = dict(self.card, text=text)
                result = self.review([combined])["results"][0]
                self.assertEqual(result["triage"], "待人审")
                self.assertIn("reply_body_prefix_unconfirmed", result["reason_codes"])
                self.assertEqual(result["candidates"][0]["text"], text)
                self.assertIsNone(result["approved_amount"])
                # A separately extracted body is sufficient, as is an exact
                # request for the complete author-written string.
                self.assertEqual(self.review([combined, self.card])["results"][0]["triage"], "成功")
                self.assertEqual(self.review([combined], [dict(self.task, comment_text=text)])["results"][0]["triage"], "成功")
                # This card already had a separate, explicitly marked reply label removed.
                # Treat any remaining prefix as the author's own text.
                self.assertEqual(self.review([dict(combined, reply_label_removed=True)])["results"][0]["triage"], "失败")

    def test_one_occurrence_verifies_two_rows_and_holds_only_settlement(self):
        tasks = [self.task, dict(self.task, case_id="b")]
        for cards in ([self.card], [self.card, dict(self.card, comment_id=None)]):
            report = self.review(cards, tasks)
            self.assertEqual(report["summary"]["triage_counts"]["成功"], 2)
            self.assertEqual(report["summary"]["settlement_pending_count"], 2)
            for row in report["results"]:
                self.assertEqual(row["settlement_status"], "pending_duplicate_claim")
                self.assertIsNone(row["approved_amount"])
                self.assertIn("matched_comment", row)
        report = self.review([self.card, dict(self.card, comment_id="second")], tasks)
        self.assertEqual(report["summary"]["settlement_pending_count"], 0)
        self.assertEqual({r["matched_comment"]["comment_id"] for r in report["results"]}, {"example", "second"})

    def test_missing_or_invalid_rates_do_not_change_the_visible_result(self):
        for value in (None, "unknown", "NaN", "-1"):
            result = self.review([self.card], [dict(self.task, expected_amount=value)])["results"][0]
            self.assertEqual((result["triage"], result["settlement_status"]), ("成功", "pending_source_amount"))
            self.assertIsNone(result["approved_amount"])
        combo = dict(self.task, comment_type="text+image", has_image=True)
        partial = self.review([self.card], [combo])["results"][0]
        self.assertEqual((partial["result_category"], partial["settlement_status"]), ("文字成功", "pending_source_amount"))
        self.assertIsNone(partial["approved_amount"])
