#!/usr/bin/env python3
"""Offline regression tests for review_comments.py."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_comments  # noqa: E402
from review_comments import compare, normalize_text  # noqa: E402


def test_shared_evidence():
    """Interleaved duplicates in multiple captures keep their original order."""
    base = {"platform": "xhs", "target_key": "shared"}
    texts = ["价格１.５元！", "价格1.5元很合适", "价格1.5元", "附带价格15元"]
    comments = [{"comment_id": str(i), "text": text, "has_image": False} for i, text in enumerate(texts)]
    captures = [
        {**base, "capture_status": "captcha", "comments": [{"text": "价格1.5元"}]},
        {**base, "capture_status": "ok", "capture_complete": False, "comments": comments[:2],
         "captured_at": "first", "evidence_path": "first.json"},
        {**base, "capture_status": "ok", "capture_complete": True, "comments": comments[2:],
         "captured_at": "second", "evidence_path": "second.json"},
    ]
    rows = [
        {**base, "case_id": str(i), "comment_type": "text", "comment_text": "价格1.5元",
         "expected_amount": "1.50", "text_match": mode}
        for i, mode in enumerate(["exact", "contains"] * 10)
    ]
    with patch.object(review_comments, "normalize_text", wraps=normalize_text) as normalize:
        results = compare(rows, captures, amount_mode="expected-only")["results"]
    for i, result in enumerate(results):
        assert result["reason_codes"] == ["duplicate_matching_comments"]
        assert [c["comment_id"] for c in result["candidates"]] == (["0", "1", "2"] if i % 2 else ["0", "2"])
    # A nonmatching evidence string is normalized once even across many rows/modes.
    assert sum(args[0] == texts[-1] for args, _ in normalize.call_args_list) == 1
    first = compare(rows[:1], captures, amount_mode="expected-only", allow_duplicates=True)["results"][0]
    assert first["matched_comment"]["comment_id"] == "0"
    assert first["capture"] == dict(captured_at="first", evidence_path="first.json", capture_complete=False)
    later = compare([dict(rows[0], comment_text=texts[-1])], captures, amount_mode="expected-only")["results"][0]
    assert later["capture"] == dict(captured_at="second", evidence_path="second.json", capture_complete=True)


def test_shared_settlement():
    base = dict(platform="xhs", target_key="n", comment_type="text", expected_amount="2.00")
    exact = dict(base, case_id="exact", comment_text="已经收到")
    contains = dict(base, case_id="contains", comment_text="收到", text_match="contains")
    comment = dict(text="已经收到", observed_amount="2", has_image=False)
    capture = dict(platform="小红书", target_key="n", capture_status="ok", capture_complete=True)
    code = "evidence_claimed_by_multiple_rows"
    for identifier in ({}, {"comment_id": 17}):
        source = [{**capture, "comments": [{**comment, **identifier}]}]
        for mode in ("observed", "expected-only"):
            for allow in (False, True):
                for rows in ([exact, contains], [contains, exact], [exact, dict(exact, case_id="copy")]):
                    results = compare(rows, source, amount_mode=mode, allow_duplicates=allow)["results"]
                    assert all(r["status"] == "needs_review" and r["approved_amount"] is None
                               and r["reason_codes"] == [code] for r in results), results
    second = dict(comment, text="已经收到另一件", comment_id="b", evidence_path="b.png")
    first = dict(comment, comment_id="a")
    # The broad match can use b, leaving a for the narrow match.
    result = compare([contains, exact], [{**capture, "comments": [first, second]}],
                     allow_duplicates=True)["results"]
    assert [r["matched_comment"]["comment_id"] for r in result] == ["b", "a"]
    assert result[0]["capture"]["evidence_path"] == "b.png"
    sources = [{**capture, "captured_at": "a-time", "evidence_path": "a.json", "comments": [first]},
               {**capture, "captured_at": "b-time", "evidence_path": "b.json", "capture_complete": False,
                "comments": [{k: v for k, v in second.items() if k != "evidence_path"}]}]
    result = compare([contains, exact], sources, allow_duplicates=True)["results"]
    assert [r["capture"] for r in result] == [
        dict(captured_at="b-time", evidence_path="b.json", capture_complete=False),
        dict(captured_at="a-time", evidence_path="a.json", capture_complete=True),
    ]
    # An alternate that fails the observed amount check cannot release the conflict.
    second["observed_amount"] = "3"
    result = compare([contains, exact], [{**capture, "comments": [first, second]}],
                     allow_duplicates=True)["results"]
    assert all(r["reason_codes"] == [code] for r in result)
    for ids, status in (([17, "17"], "needs_review"), (["a", "b"], "verified"), ([None, None], "verified")):
        # Distinct captures can have the same index; only an explicit shared ID links their records.
        observations = [{**capture, "comments": [{**comment, "comment_id": cid, "dom_index": 0, "text": text}]}
                        for cid, text in zip(ids, ("已收", "收到"))]
        results = compare([dict(exact, comment_text="已收"), dict(contains, text_match="exact")],
                          observations, amount_mode="expected-only")["results"]
        assert [r["status"] for r in results] == [status, status]
    # An explicitly priced partial and a full row cannot spend the same evidence twice.
    partial = dict(exact, case_id="mixed", comment_type="text+image", partial_text_amount="1.50")
    two_texts = [{**capture, "comments": [dict(comment, comment_id="a"), dict(comment, comment_id="b")]}]
    duplicate = compare([partial], two_texts, amount_mode="expected-only")["results"][0]
    assert duplicate["reason_codes"] == ["duplicate_matching_comments"] and duplicate["approved_amount"] is None
    permissive = compare([partial], two_texts, amount_mode="expected-only", allow_duplicates=True)["results"][0]
    assert permissive["status"] == "partial_text" and permissive["approved_amount"] == "1.50"
    source = [{**capture, "comments": [comment]}]
    result = compare([exact, partial], source, amount_mode="expected-only")["results"]
    assert all(r["reason_codes"] == [code] for r in result)
    result = compare([exact, {**partial, "partial_text_amount": None}], source, amount_mode="expected-only")["results"]
    assert [(r["status"], r["approved_amount"]) for r in result] == [("verified", "2.00"), ("partial_text", None)]
    pic = dict(comment, text="", has_image=True)
    result = compare([dict(exact, comment_type="image", comment_text=""), dict(partial, partial_image_amount="1.00")],
                     [{**capture, "comments": [pic]}], amount_mode="expected-only")["results"]
    assert all(r["reason_codes"] == [code] for r in result)
    # A shared comment ID on a different platform or target remains independent.
    places = [("xhs", "n"), ("xhs", "m"), ("douyin", "n")]
    rows = [dict(exact, case_id=f"{p}-{t}", platform=p, target_key=t) for p,t in places]
    source = [dict(capture, platform=p, target_key=t, comments=[first]) for p,t in places]
    assert all(r["status"] == "verified" for r in compare(rows, source)["results"])
    blank = [dict(exact, case_id=str(i), comment_text=body) for i, body in enumerate(["", "！", "[笑R]"])]
    result = compare(blank + [exact], [{**capture, "comments": [first]}], amount_mode="expected-only")["results"]
    assert all(r["reason_codes"] == ["invalid_or_contradictory_comment_type"] for r in result[:-1])
    assert result[-1]["approved_amount"] == "2.00"


def run():
    test_shared_evidence()
    test_shared_settlement()
    assert normalize_text('@点点. 有什么作用') == normalize_text('@点点 有什么作用')
    assert normalize_text('可以。') == normalize_text('可以.')
    assert normalize_text('1.5元') != normalize_text('15元')
    assert normalize_text("喝了比不喝强[暗中观察R]") == normalize_text("喝了比不喝强")
    expected = [
        {"case_id": "text-ok", "platform": "douyin", "target_key": "v1", "comment_text": "已 收到！", "comment_type": "text", "expected_amount": "99.90"},
        {"case_id": "image-ok", "platform": "xiaohongshu", "target_key": "n1", "comment_text": "", "comment_type": "image", "expected_amount": "1.2万"},
        {"case_id": "mixed-ok", "platform": "douyin", "target_key": "v1", "comment_text": "晒单完成", "comment_type": "text+image", "expected_amount": "100"},
        {"case_id": "amount-bad", "platform": "douyin", "target_key": "v1", "comment_text": "金额不对", "comment_type": "text", "expected_amount": "50"},
        {"case_id": "ocr-missing", "platform": "xiaohongshu", "target_key": "n1", "comment_text": "", "comment_type": "image", "expected_amount": "20"},
        {"case_id": "not-found", "platform": "douyin", "target_key": "v1", "comment_text": "不存在的评论", "comment_type": "text", "expected_amount": "1"},
        {"case_id": "blocked", "platform": "douyin", "target_key": "v2", "comment_text": "任意", "comment_type": "text", "expected_amount": "1"},
    ]
    observations = [
        {
            "platform": "douyin", "target_key": "v1", "capture_status": "ok", "capture_complete": True,
            "comments": [
                {"comment_id": "c1", "text": "已收到", "comment_type": "text", "observed_amount": "¥99.90"},
                {"comment_id": "c2", "text": "晒单完成", "has_image": True, "observed_amount": "100元"},
                {"comment_id": "c3", "text": "金额不对", "comment_type": "text", "observed_amount": "40"},
            ],
        },
        {
            "platform": "xiaohongshu", "target_key": "n1", "capture_status": "ok", "capture_complete": True,
            "comments": [
                {"comment_id": "c4", "text": "", "has_image": True, "observed_amount": "1.2万", "amount_source": "ocr"},
                {"comment_id": "c5", "text": "", "has_image": True},
            ],
        },
        {"platform": "douyin", "target_key": "v2", "capture_status": "captcha", "capture_complete": False, "comments": []},
    ]
    report = compare(expected, observations)
    statuses = {item["case_id"]: item["status"] for item in report["results"]}
    expected_statuses = {
        "text-ok": "verified",
        "image-ok": "needs_review",  # duplicate image candidates are intentionally not auto-approved
        "mixed-ok": "verified",
        "amount-bad": "mismatch",
        "ocr-missing": "needs_review",
        "not-found": "not_found",
        "blocked": "blocked",
    }
    assert statuses == expected_statuses, statuses
    amounts = {item["case_id"]: item.get("approved_amount") for item in report["results"]}
    assert amounts == {
        "text-ok": "99.90",
        "image-ok": None,
        "mixed-ok": "100.00",
        "amount-bad": "0.00",
        "ocr-missing": None,
        "not-found": "0.00",
        "blocked": None,
    }, amounts
    assert report["summary"]["counts"] == {
        "verified": 2, "not_found": 1, "mismatch": 1,
        "partial_text": 0, "partial_image": 0, "needs_review": 2, "blocked": 1,
    }
    payout_report = compare(
        expected,
        observations,
        amount_mode="expected-only",
        allow_duplicates=True,
    )
    payout_statuses = {item["case_id"]: item["status"] for item in payout_report["results"]}
    assert payout_statuses["text-ok"] == "verified"
    assert payout_statuses["image-ok"] == "verified"
    assert payout_statuses["amount-bad"] == "verified"
    assert payout_statuses["not-found"] == "not_found"
    partial_expected = [
        {"case_id": "partial-found", "platform": "xiaohongshu", "target_key": "p1", "comment_text": "已经看到", "comment_type": "text", "expected_amount": "1.5"},
        {"case_id": "partial-missing", "platform": "xiaohongshu", "target_key": "p1", "comment_text": "尚未翻到", "comment_type": "text", "expected_amount": "1.5"},
    ]
    partial_observations = [{
        "platform": "xiaohongshu", "target_key": "p1", "capture_status": "ok", "capture_complete": False,
        "comments": [{"comment_id": "pc1", "text": "已经看到", "comment_type": "text"}],
    }]
    partial_report = compare(partial_expected, partial_observations, amount_mode="expected-only")
    partial_statuses = {item["case_id"]: item["status"] for item in partial_report["results"]}
    assert partial_statuses == {"partial-found": "verified", "partial-missing": "blocked"}

    combo_expected = [
        {"case_id": "combo-both", "platform": "douyin", "target_key": "cb", "comment_text": "图文都在", "comment_type": "text+image", "expected_amount": "2"},
        {"case_id": "combo-text", "platform": "douyin", "target_key": "ct", "comment_text": "只有文字", "comment_type": "text+image", "expected_amount": "2", "partial_text_amount": "1.5", "partial_image_amount": "1.5"},
        {"case_id": "combo-image", "platform": "douyin", "target_key": "ci", "comment_text": "应该有文字", "comment_type": "text+image", "expected_amount": "2", "partial_text_amount": "1.5", "partial_image_amount": "1.5"},
        {"case_id": "combo-none", "platform": "douyin", "target_key": "cn", "comment_text": "都没有", "comment_type": "text+image", "expected_amount": "2"},
        {"case_id": "combo-incomplete", "platform": "douyin", "target_key": "cx", "comment_text": "只加载到文字", "comment_type": "text+image", "expected_amount": "2", "source_sheet": "示例", "source_row": 9},
        {"case_id": "combo-image-unknown", "platform": "douyin", "target_key": "cu", "comment_text": "图片状态未知", "comment_type": "text+image", "expected_amount": "2"},
    ]
    combo_observations = [
        {"platform": "douyin", "target_key": "cb", "capture_status": "ok", "capture_complete": True, "comments": [{"text": "图文都在", "has_image": True}]},
        {"platform": "douyin", "target_key": "ct", "capture_status": "ok", "capture_complete": True, "comments": [{"text": "只有文字", "has_image": False}]},
        {"platform": "douyin", "target_key": "ci", "capture_status": "ok", "capture_complete": True, "comments": [{"text": "", "has_image": True}]},
        {"platform": "douyin", "target_key": "cn", "capture_status": "ok", "capture_complete": True, "comments": []},
        {"platform": "douyin", "target_key": "cx", "capture_status": "ok", "capture_complete": False, "comments": [{"text": "只加载到文字", "has_image": False}]},
        {"platform": "douyin", "target_key": "cu", "capture_status": "ok", "capture_complete": True, "image_detection_complete": False, "comments": [{"text": "图片状态未知"}]},
    ]
    combo_report = compare(combo_expected, combo_observations, amount_mode="expected-only")
    combo_results = {item["case_id"]: (item["status"], item["result_category"], item["triage"]) for item in combo_report["results"]}
    assert combo_results == {
        "combo-both": ("verified", "图字成功", "成功"),
        "combo-text": ("partial_text", "文字成功", "成功"),
        "combo-image": ("partial_image", "图片成功", "成功"),
        "combo-none": ("not_found", "失败", "失败"),
        "combo-incomplete": ("blocked", "待人审", "待人审"),
        "combo-image-unknown": ("needs_review", "待人审", "待人审"),
    }
    assert combo_report["summary"]["triage_counts"] == {"成功": 3, "失败": 1, "待人审": 2}
    combo_amounts = {item["case_id"]: item.get("approved_amount") for item in combo_report["results"]}
    assert combo_amounts == {
        "combo-both": "2.00",
        "combo-text": "1.50",
        "combo-image": "1.50",
        "combo-none": "0.00",
        "combo-incomplete": None,
        "combo-image-unknown": None,
    }, combo_amounts
    incomplete = next(item for item in combo_report["results"] if item["case_id"] == "combo-incomplete")
    assert incomplete["source_sheet"] == "示例" and incomplete["source_row"] == 9
    assert incomplete["reason_codes"] == ["capture_incomplete_image_not_seen"]
    unknown = next(item for item in combo_report["results"] if item["case_id"] == "combo-image-unknown")
    assert unknown["reason_codes"] == ["comment_image_presence_unknown"]
    print("ok: comment review regression tests")


if __name__ == "__main__":
    run()
