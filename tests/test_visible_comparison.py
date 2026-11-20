"""Synthetic visible-comment cases for strict matching and negative evidence."""
import unittest

from huozhiji_audit import review
from huozhiji_audit._engine.review_comments import compare, normalize_text


class VisibleComparisonTests(unittest.TestCase):
    def setUp(self):
        self.row = dict(case_id="r", platform="xiaohongshu", target_key="n", comment_type="text",
                        comment_text="一条评论", expected_amount="2")
        self.seen = dict(comment_id="c", text="一条评论", has_image=False)

    def cap(self, comments, **fields):
        return {**dict(platform="xhs", target_key="n", capture_status="ok", capture_complete=True,
                       image_detection_complete=True, comments=comments), **fields}

    def outcome(self, *captures, row=None):
        return review([row or self.row], list(captures))["results"][0]

    def test_display_variants_preserve_distinct_words_emoji_and_amounts(self):
        variants = [("一条评论", "一条\u200b评\ufeff论"), ("谢谢❤", "谢谢❤️"),
                    ("1.5元", "1.\u20605元"), ("☀︎", "☀️")]
        for expected, observed in variants:
            with self.subTest(expected=expected, observed=observed):
                r = self.outcome(self.cap([dict(self.seen, text=observed)]),
                                 row=dict(self.row, comment_text=expected))
                self.assertEqual(r["status"], "verified")
        for a, b in (("👩‍💻", "👩💻"), ("👍", "👍🏽"), ("好", "不好"), ("1.5", "15")):
            self.assertNotEqual(normalize_text(a), normalize_text(b))

    def test_sticker_only_tasks_and_numeric_signs_are_distinguishable(self):
        for wanted, visible, verdict in (
                ("[笑哭R]", "[笑哭R]", "成功"), ("[笑哭R]", "[捂脸R]", "失败"),
                ("[笑哭R]", "笑哭r", "失败"), ("好[笑哭R]", "好", "成功"),
                ("-2度", "2度", "失败"), ("−2度", "-2度", "成功"),
                ("2%", "2", "失败"), ("3-5天", "35天", "失败")):
            with self.subTest(wanted=wanted, visible=visible):
                result = self.outcome(self.cap([dict(self.seen, text=visible)]), row=dict(self.row, comment_text=wanted))
                self.assertEqual(result["triage"], verdict)

    def test_stable_id_repeated_is_one_candidate_without_merging_different_evidence(self):
        first = self.cap([dict(self.seen, evidence_path="a.png")])
        second = self.cap([dict(self.seen, evidence_path="b.png", dom_index=8)])
        self.assertEqual(self.outcome(first, second)["status"], "verified")
        other = self.cap([dict(self.seen, comment_id="different")])
        self.assertEqual(self.outcome(first, other)["triage"], "成功")
        no_id = self.cap([dict(self.seen, comment_id=None), dict(self.seen, comment_id=None)])
        self.assertEqual(self.outcome(no_id)["triage"], "成功")
        row = dict(self.row, comment_type="text+image", partial_text_amount="1.00")
        self.assertEqual(self.outcome(first, second, row=row)["approved_amount"], "1.00")
        img = dict(self.seen, text="", has_image=True)
        photo = self.cap([img])
        self.assertEqual(self.outcome(photo, photo, row=dict(self.row, comment_text="", comment_type="image"))["status"], "verified")
        conflict = self.cap([dict(self.seen, observed_amount="3")])
        evidence = [self.cap([dict(self.seen, observed_amount="2")]), conflict]
        self.assertEqual(compare([self.row], evidence)["results"][0]["triage"], "待人审")
        amount_row = dict(self.row, comment_text="样例2元")
        captions = [dict(self.seen, text=t) for t in ("样例2元", "样例-2元")]
        self.assertEqual(compare([amount_row], [self.cap(captions)])["results"][0]["status"], "verified")

    def test_observation_text_alias_is_consistent_for_type_matching_and_money(self):
        record = dict(comment_id="c", comment_text="收到2元", has_image=False)
        row = dict(self.row, comment_text="收到2元")
        self.assertEqual(self.outcome(self.cap([record]), row=row)["status"], "verified")
        self.assertEqual(compare([row], [self.cap([record])])["results"][0]["status"], "verified")
        wrong = dict(row, comment_text="else", comment_type="text+image", partial_image_amount="1")
        record["has_image"] = True
        self.assertEqual(self.outcome(self.cap([record]), row=wrong)["status"], "not_found")

    def test_incomplete_wrong_type_or_amount_is_not_a_definite_failure(self):
        wrong_type = self.cap([dict(self.seen, has_image=True)], capture_complete=False)
        self.assertEqual(self.outcome(wrong_type)["triage"], "成功")
        full = dict(wrong_type, capture_complete=True)
        self.assertEqual(self.outcome(full)["triage"], "成功")
        wrong_amount = self.cap([dict(self.seen, observed_amount="3")], capture_complete=False)
        r = compare([self.row], [wrong_amount])["results"][0]
        self.assertEqual(r["triage"], "待人审")
        self.assertIsNone(r["approved_amount"])

    def test_older_xhs_capture_can_prove_presence_not_absence(self):
        for legacy in ("xhs-replies-20261118-v4", "xhs-visible-end-v5", "xhs-confirmed-expansion-v6"):
            with self.subTest(legacy=legacy):
                original = self.cap([], parser_revision=legacy)
                r = self.outcome(original)
                self.assertEqual(r["triage"], "待人审")
                self.assertIn("legacy_xhs_visible_recheck_required", r["reason_codes"])
                self.assertEqual(self.outcome(dict(original, comments=[self.seen]))["status"], "verified")
                self.assertEqual(self.outcome(original, self.cap([]))["status"], "not_found")
                dy = dict(original, platform="douyin")
                self.assertEqual(self.outcome(dy, row=dict(self.row, platform="douyin"))["status"], "not_found")

    def test_malformed_comment_list_cannot_establish_absence(self):
        for records in (None, "unreadable", [None], [{}], [{"text": ["broken"]}]):
            with self.subTest(records=records):
                self.assertEqual(self.outcome(self.cap(records))["triage"], "待人审")
        self.assertEqual(self.outcome(self.cap([{}, self.seen]))["triage"], "成功")
        self.assertEqual(self.outcome(self.cap([]))["triage"], "失败")
        photo_task = dict(self.row, comment_type="image", comment_text="")
        photo = dict(comment_id="photo", text=None, has_image=True)
        self.assertEqual(self.outcome(self.cap([photo]), row=photo_task)["triage"], "成功")

    def test_old_douyin_reader_can_prove_presence_but_missed_replies(self):
        row = dict(self.row, platform="douyin")
        old = self.cap([], platform="douyin", parser_revision="dy-body-20260930-v2")
        self.assertEqual(self.outcome(old, row=row)["triage"], "待人审")
        self.assertEqual(self.outcome(dict(old, comments=[self.seen]), row=row)["triage"], "成功")
        self.assertEqual(self.outcome(dict(old, parser_revision="dy-visible-replies-v3"), row=row)["triage"], "失败")

    def test_v7_needs_a_rendered_end_to_prove_absence(self):
        old = self.cap([], parser_revision="xhs-visible-controls-v7")
        self.assertEqual(self.outcome(old)["triage"], "待人审")
        self.assertEqual(self.outcome(dict(old, capture_complete_source="visible_comment_list_end"))["triage"], "失败")
