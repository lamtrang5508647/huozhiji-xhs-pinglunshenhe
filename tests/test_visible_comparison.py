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

    def test_stable_id_repeated_is_one_candidate_without_merging_different_evidence(self):
        first = self.cap([dict(self.seen, evidence_path="a.png")])
        second = self.cap([dict(self.seen, evidence_path="b.png", dom_index=8)])
        self.assertEqual(self.outcome(first, second)["status"], "verified")
        other = self.cap([dict(self.seen, comment_id="different")])
        self.assertIn("duplicate_matching_comments", self.outcome(first, other)["reason_codes"])
        no_id = self.cap([dict(self.seen, comment_id=None), dict(self.seen, comment_id=None)])
        self.assertIn("duplicate_matching_comments", self.outcome(no_id)["reason_codes"])
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
        self.assertIn("duplicate_matching_comments",
                      compare([amount_row], [self.cap(captions)])["results"][0]["reason_codes"])

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
        self.assertEqual(self.outcome(wrong_type)["triage"], "待人审")
        full = dict(wrong_type, capture_complete=True)
        self.assertEqual(self.outcome(full)["triage"], "失败")
        wrong_amount = self.cap([dict(self.seen, observed_amount="3")], capture_complete=False)
        r = compare([self.row], [wrong_amount])["results"][0]
        self.assertEqual(r["triage"], "待人审")
        self.assertIsNone(r["approved_amount"])

    def test_older_xhs_capture_can_prove_presence_not_absence(self):
        legacy = "xhs-replies-20261118-v4"
        original = self.cap([], parser_revision=legacy)
        r = self.outcome(original)
        self.assertEqual(r["triage"], "待人审")
        self.assertIn("legacy_xhs_visible_recheck_required", r["reason_codes"])
        self.assertEqual(self.outcome(dict(original, comments=[self.seen]))["status"], "verified")
        self.assertEqual(self.outcome(original, self.cap([]))["status"], "not_found")
        dy = dict(original, platform="douyin")
        self.assertEqual(self.outcome(dy, row=dict(self.row, platform="douyin"))["status"], "not_found")
