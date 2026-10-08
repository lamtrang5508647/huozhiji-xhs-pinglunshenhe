"""Shared settlement invariants for CLI, SDK and workbook writers."""
from decimal import Decimal, InvalidOperation
from typing import Any


def validate_results(rows: list[dict[str, Any]], results: list[dict[str, Any]]) -> Decimal:
    expected_ids = [row.get("case_id") for row in rows]
    actual_ids = [item.get("case_id") for item in results]
    if any(not isinstance(value, str) or not value for value in expected_ids + actual_ids):
        raise RuntimeError("report_row_identity_mismatch")
    if (not rows or not all(expected_ids) or len(set(expected_ids)) != len(expected_ids)
            or len(set(actual_ids)) != len(actual_ids) or set(expected_ids) != set(actual_ids)):
        raise RuntimeError("report_row_identity_mismatch")
    payout = Decimal("0")
    for item in results:
        triage, category = item.get("triage"), item.get("result_category")
        if triage not in ("成功", "失败", "待人审") or category not in ("成功", "失败", "待人审", "图字成功", "文字成功", "图片成功"):
            raise RuntimeError("report_category_missing_or_invalid")
        if ((triage == "成功" and category not in ("成功", "图字成功", "文字成功", "图片成功"))
                or (triage == "失败" and category != "失败")
                or (triage == "待人审" and category not in ("待人审", "文字成功", "图片成功"))):
            raise RuntimeError("report_category_triage_conflict")
        value = item.get("approved_amount")
        if triage == "待人审":
            if value not in (None, ""):
                raise RuntimeError("manual_review_amount_must_be_blank")
            continue
        if value in (None, ""):
            raise RuntimeError("settled_amount_missing")
        try:
            amount = Decimal(str(value))
        except InvalidOperation as exc:
            raise RuntimeError("settled_amount_invalid") from exc
        if not amount.is_finite() or amount < 0 or (triage == "失败" and amount != 0) or amount != amount.quantize(Decimal("0.01")):
            raise RuntimeError("settled_amount_invalid")
        payout += amount
    return payout.quantize(Decimal("0.01"))
