"""Select successful rows for recheck and reconcile them with untouched source rows."""
from decimal import Decimal, InvalidOperation

if __package__:
    from .review_comments import compare
else:
    from review_comments import compare


def rows_to_review(rows):
    if any("prior_review" in row for row in rows):
        # Intake marks every row of a previously audited workbook, including blank results.
        # An unmarked row can come from a separate first-pass workbook in a combined JSON batch.
        return [row for row in rows if "prior_review" not in row or row["prior_review"].get("triage") == "成功"]
    return rows


def carried_result(row):
    prior = row.get("prior_review", {})
    triage = prior.get("triage")
    if triage not in ("失败", "待人审"):
        triage = "未复核"
    amount = None
    if triage == "失败":
        try:
            if Decimal(str(prior.get("amount"))).is_zero():
                amount = "0.00"
        except InvalidOperation:
            pass
    return {
        **{k: row[k] for k in ("case_id", "platform", "target_key", "source_file", "source_sheet", "source_row") if k in row},
        "expected_type": row.get("comment_type"), "status": "carried_forward",
        "review_action": "carried_forward", "previous_result": prior.get("raw_category", ""),
        "reason_codes": ["not_previously_successful"], "triage": triage,
        "result_category": prior.get("category") or prior.get("raw_category", ""), "approved_amount": amount,
    }


def merge_audit_results(rows, active_report):
    summary = active_report["summary"]
    recheck = any("prior_review" in row for row in rows)
    scope = {"mode": "success_recheck" if recheck else "first_pass",
             "reviewed_rows": len(active_report["results"]), "carried_rows": 0}
    if not recheck:
        return {"summary": {**summary, "audit_pass": scope}, "results": active_report["results"]}
    active = {r["case_id"]: r for r in active_report["results"]}
    if len(active) != len(active_report["results"]) or set(active) != {r["case_id"] for r in rows_to_review(rows)}:
        raise ValueError("recheck_result_identity_mismatch")
    triage_counts = summary["triage_counts"].copy()
    scope["reviewed_triage_counts"] = triage_counts.copy()
    categories = summary["result_category_counts"].copy()
    results = []
    for row in rows:
        if row["case_id"] in active:
            result = active[row["case_id"]]
            if "prior_review" in row:
                results.append({**result, "review_action": "rechecked",
                                "previous_result": row["prior_review"].get("raw_category", "")})
            else:
                results.append({**result, "review_action": "first_pass"})
                scope["first_pass_rows"] = scope.get("first_pass_rows", 0) + 1
        else:
            result = carried_result(row)
            results.append(result)
            triage = result["triage"]
            triage_counts[triage] = triage_counts.get(triage, 0) + 1
            label = result["result_category"] if result["result_category"] in categories else "未复核"
            categories[label] = categories.get(label, 0) + 1
            scope["carried_rows"] += 1
    return {"summary": {**summary, "total": len(rows), "audit_pass": scope, "triage_counts": triage_counts,
                        "counts": {**summary["counts"], "carried_forward": scope["carried_rows"]},
                        "result_category_counts": categories}, "results": results}


def compare_audit_rows(rows, captures, *, require_reply_parent=False, amount_mode="expected-only", allow_duplicates=False):
    active = rows_to_review(rows)
    report = compare(active, captures if active else (), amount_mode=amount_mode, allow_duplicates=allow_duplicates,
                     require_reply_parent=require_reply_parent,
                     reply_context_rows=rows if active is not rows else None)
    return merge_audit_results(rows, report)
