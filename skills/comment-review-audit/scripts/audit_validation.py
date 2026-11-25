"""Shared settlement invariants for CLI, SDK and workbook writers."""
from decimal import Decimal, InvalidOperation
from typing import Any
if __package__:
    from .audit_pass import carried_result
else:
    from audit_pass import carried_result


PENDING_SETTLEMENT_REASONS = {
    "pending_duplicate_claim": {"evidence_claimed_by_multiple_rows"},
    "pending_source_amount": {"invalid_expected_amount", "missing_component_amount"},
    "pending_specified_image": {"specified_image_unconfirmed"},
}


def validate_results(rows: list[dict[str, Any]], results: list[dict[str, Any]]) -> Decimal:
    expected_ids = [row.get("case_id") for row in rows]
    actual_ids = [item.get("case_id") for item in results]
    if any(not isinstance(value, str) or not value for value in expected_ids + actual_ids):
        raise RuntimeError("report_row_identity_mismatch")
    if (not rows or not all(expected_ids) or len(set(expected_ids)) != len(expected_ids)
            or len(set(actual_ids)) != len(actual_ids) or set(expected_ids) != set(actual_ids)):
        raise RuntimeError("report_row_identity_mismatch")
    payout = Decimal("0")
    sources = {row["case_id"]: row for row in rows}
    for item in results:
        source = sources[item["case_id"]]
        for field in ("source_file", "source_sheet", "source_row"):
            if field in item and field in source and item[field] != source[field]:
                raise RuntimeError("report_source_location_mismatch")
        if item.get("status") == "carried_forward" or item.get("review_action") == "carried_forward":
            if "prior_review" not in source or source["prior_review"].get("triage") == "成功":
                raise RuntimeError("invalid_carried_forward_row")
            retained = carried_result(source)
            if any(item.get(k) != retained[k] for k in ("status", "review_action", "triage", "result_category", "approved_amount")):
                raise RuntimeError("carried_forward_value_changed")
            continue
        if "prior_review" in source and source["prior_review"].get("triage") != "成功":
            raise RuntimeError("missing_carried_forward_row")
        triage, category = item.get("triage"), item.get("result_category")
        if triage not in ("成功", "失败", "待人审") or category not in ("成功", "失败", "待人审", "原帖不可访问", "图字成功", "文字成功", "图片成功"):
            raise RuntimeError("report_category_missing_or_invalid")
        if category == "原帖不可访问":
            capture = item.get("capture") or {}
            if (item.get("status") != "blocked" or capture.get("capture_status") != "inaccessible"
                    or capture.get("target_page_state") != "post_unavailable"):
                raise RuntimeError("unconfirmed_post_unavailable")
        if ((triage == "成功" and category not in ("成功", "图字成功", "文字成功", "图片成功"))
                or (triage == "失败" and category != "失败")
                or (triage == "待人审" and category not in ("待人审", "原帖不可访问", "文字成功", "图片成功"))):
            raise RuntimeError("report_category_triage_conflict")
        value = item.get("approved_amount")
        reasons = PENDING_SETTLEMENT_REASONS.get(item.get("settlement_status"))
        if reasons:
            if item["settlement_status"] == "pending_specified_image":
                valid = (triage, category) in (("成功", "文字成功"), ("待人审", "待人审"))
            else:
                valid = triage == "成功"
            if not valid or value not in (None, "") or not reasons.intersection(item.get("reason_codes", [])):
                raise RuntimeError("invalid_pending_settlement")
            continue
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
