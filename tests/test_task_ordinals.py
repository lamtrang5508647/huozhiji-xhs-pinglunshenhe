import unittest

from huozhiji_audit import review
from huozhiji_audit._engine.review_comments import _reply_parents
from huozhiji_audit._engine.table_to_expected import convert, parse_maps


class TaskOrdinalTests(unittest.TestCase):
    def rows(self, ordinals=(1, 3, 4), *, header="账号", mapped=True):
        records = [
            {header: ordinal, "笔记ID": "fixture", "平台": "小红书", "评论": body, "回复内容": reply, "金额": "2"}
            for ordinal, (body, reply) in zip(ordinals, (("first", ""), ("third", ""), ("回复第3条", "answer")))
        ]
        return convert(records, parse_maps([f"task_ordinal={header}"]) if mapped else {}, "fixture.xlsx", "Sheet")

    def test_only_mapped_or_dedicated_ordinal_columns_resolve_number_gaps(self):
        rows = self.rows()
        self.assertEqual([r["task_ordinal"] for r in rows], [1, 3, 4])
        self.assertEqual(_reply_parents(rows), {2: "third"})
        self.assertEqual(_reply_parents(self.rows(header="评论序号", mapped=False)), {2: "third"})
        legacy = self.rows(mapped=False)
        self.assertTrue(all("task_ordinal" not in r for r in legacy))
        self.assertEqual(_reply_parents(legacy), {2: None})

    def test_invalid_missing_duplicate_or_self_reference_stays_unresolved(self):
        for values in (("", "", ""), (1, "", 4), (3, 3, 4), (1, 2, 3), (1, "invalid", 4)):
            with self.subTest(values=values):
                rows = self.rows(values)
                self.assertEqual(_reply_parents(rows), {2: None})
                self.assertNotIn("invalid", str(rows))
        rows = self.rows()
        rows[1]["reply_to"] = "回复第1条"
        self.assertIsNone(_reply_parents(rows)[2])
        rows = self.rows()
        rows[0]["comment_text"] = rows[1]["comment_text"]
        self.assertIsNone(_reply_parents(rows)[2])

    def test_same_target_in_separate_blocks_cannot_supply_a_parent(self):
        rows = self.rows()
        for row in rows[1:]:
            row["source_row"] += 5
        rows[2]["reply_to"] = "回复第1条"
        self.assertIsNone(_reply_parents(rows)[2])
        rows[2]["reply_to"] = "回复第3条"
        self.assertEqual(_reply_parents(list(reversed(rows)))[0], "third")
        for field in ("source_file", "source_sheet", "target_key", "platform"):
            with self.subTest(field=field):
                isolated = self.rows()
                isolated[1][field] = "separate"
                self.assertIsNone(_reply_parents(isolated)[2])

    def test_source_number_is_separate_from_direct_platform_evidence(self):
        rows = self.rows()
        base = {"platform": "xiaohongshu", "target_key": "fixture", "capture_status": "ok",
                "capture_complete": True, "image_detection_complete": True}
        root = {"comment_id": "p", "text": "third", "has_image": False}
        message = {"comment_id": "c", "text": "answer", "has_image": False}
        wrong = review(rows, [{**base, "comments": [root, message]}])["results"][2]
        self.assertEqual((wrong["triage"], wrong["approved_amount"]), ("待人审", None))
        correct = review(rows, [{**base, "comments": [root, {**message, "parent_comment_id": "p"}]}])["results"][2]
        self.assertEqual((correct["triage"], correct["approved_amount"]), ("成功", "2.00"))
