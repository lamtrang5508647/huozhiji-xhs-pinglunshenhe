#!/usr/bin/env python3
"""Offline regression tests for review_comments.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_comments import compare, normalize_text  # noqa: E402


def run():
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
