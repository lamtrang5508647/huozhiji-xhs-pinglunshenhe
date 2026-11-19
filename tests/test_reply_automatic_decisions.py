import unittest

from huozhiji_audit import review


class AutomaticReplyDecisionTests(unittest.TestCase):
    def setUp(self):
        self.root = dict(case_id="root", platform="xiaohongshu", target_key="note",
                         source_row=2, comment_text="question", expected_amount="1")
        self.reply = dict(self.root, case_id="reply", source_row=3, comment_text="answer", reply_to="回复第1条")
        self.parent = dict(comment_id="p", text="question", has_image=False)
        self.cap = dict(platform="xiaohongshu", target_key="note", capture_status="ok", capture_complete=True,
                        image_detection_complete=True, capture_complete_source="visible_comment_list_end",
                        comments=[self.parent])

    def outcome(self, *captures, row=None):
        return review([self.root, row or self.reply], list(captures))["results"][1]

    def test_exhaustive_visible_list_with_the_required_parent_proves_missing_text_reply(self):
        result = self.outcome(self.cap)
        self.assertEqual((result["status"], result["approved_amount"]), ("not_found", "0.00"))
        self.assertIn("reply_text_absent_after_visible_end", result["reason_codes"])
        self.assertEqual(result["capture"]["capture_complete_source"], "visible_comment_list_end")

    def test_no_negative_from_wrong_level_missing_parent_incomplete_or_conflicting_views(self):
        wrong = dict(comment_id="child", text="answer", has_image=False, parent_comment_id="other")
        for changes in (
                dict(capture_complete=False), dict(capture_complete_source=None), dict(image_detection_complete=False),
                dict(parser_revision="xhs-replies-20261118-v4"), dict(capture_status="captcha"), dict(comments=[]),
                dict(comments=[dict(self.parent, deleted=True)]),
                dict(comments=[dict(self.parent, parent_comment_id="another")]),
                dict(comments=[self.parent, dict(comment_id="x", text="回复 @其他人 answer", has_image=False)]),
                dict(comments=[self.parent, dict(self.parent, comment_id="q")]),
                dict(comments=[self.parent, wrong])):
            with self.subTest(changes=changes):
                self.assertEqual(self.outcome(dict(self.cap, **changes))["triage"], "待人审")
        other_view = dict(self.cap, capture_complete=False, comments=[wrong])
        self.assertEqual(self.outcome(self.cap, other_view)["triage"], "待人审")
        self.assertEqual(self.outcome(self.cap, row=dict(self.reply, reply_to="回复第9条"))["triage"], "待人审")
        self.assertEqual(self.outcome(self.cap, row=dict(self.reply, comment_type="text+image"))["triage"], "待人审")

    def test_same_stable_reply_in_two_supported_observation_shapes_is_one_match(self):
        first = dict(comment_id="reply", text="answer", has_image=False, parent_comment_id="p")
        second = dict(comment_id="reply", comment_text="an\u200bswer", comment_type="文字",
                      parent_comment_id="p")
        original = dict(self.cap, comments=[self.parent, first])
        repeat = dict(self.cap, comments=[dict(self.parent), second])
        self.assertEqual(self.outcome(original, repeat)["status"], "verified")
        for change in (dict(comment_id="distinct"), dict(observed_amount="2")):
            amended = dict(repeat, comments=[dict(self.parent), dict(second, **change)])
            self.assertEqual(self.outcome(original, amended)["triage"], "待人审")

    def test_verified_other_parent_together_with_the_intended_parent_proves_wrong_placement(self):
        other = dict(self.parent, comment_id="other", text="another question")
        wrong = dict(comment_id="r", text="answer", has_image=False, parent_comment_id="other")
        both = dict(self.cap, comments=[self.parent, other, wrong])
        result = self.outcome(both)
        self.assertEqual((result["status"], result["approved_amount"]), ("mismatch", "0.00"))
        self.assertIn("reply_confirmed_under_other_parent", result["reason_codes"])
        self.assertEqual(result["capture"]["observed_parents"], [{"comment_id":"other", "text":"another question"}])
        second = dict(self.parent, comment_id="second", text="other thread")
        more = both["comments"] + [second, dict(wrong, comment_id="q", parent_comment_id="second")]
        self.assertEqual(len(self.outcome(dict(both, comments=more))["capture"]["observed_parents"]), 2)
        # A separate verified reply in the requested thread satisfies the task.
        right = dict(wrong, comment_id="right", parent_comment_id="p")
        self.assertEqual(self.outcome(dict(both, comments=both["comments"]+[right]))["triage"], "成功")
        for items in ([self.parent, wrong], [other, wrong],
                      [self.parent, dict(other, deleted=True), wrong], [self.parent, dict(other, text=""), wrong],
                      [self.parent, other, dict(wrong, comment_id=None)],
                      [self.parent, other, dict(wrong, parent_text="conflicting text")],
                      [self.parent, other, wrong, dict(wrong, comment_id="unknown", parent_comment_id=None)],
                      [self.parent, other, wrong, dict(other, text="conflicting record")]):
            self.assertEqual(self.outcome(dict(both, comments=items))["triage"], "待人审")
        for extra in (dict(capture_complete=False), dict(capture_complete_source=None),
                      dict(parser_revision="xhs-visible-end-v5")):
            self.assertEqual(self.outcome(dict(both, **extra))["triage"], "待人审")
        self.assertEqual(self.outcome(dict(self.cap, comments=[self.parent, wrong]),
                                     dict(self.cap, comments=[other]))["triage"], "待人审")
        # One physical ID cannot establish two different direct placements across snapshots.
        conflict = dict(wrong, parent_comment_id="p")
        self.assertEqual(self.outcome(both, dict(self.cap, comments=[self.parent, conflict]))["triage"], "待人审")
