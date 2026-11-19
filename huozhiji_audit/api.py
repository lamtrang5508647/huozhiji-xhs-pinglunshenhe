"""Side-effect-free conversion/comparison and explicit local workbook export."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ._engine import review_comments, table_to_expected
from ._engine.audit_validation import validate_results


def convert_table(source: str | Path, *, mapping: dict[str, str] | None = None,
                  amount_rules: dict[str, str] | None = None, sheet: str | None = None) -> dict[str, Any]:
    """Read XLSX (all product sheets by default), CSV, TSV or JSON requirements."""
    path = Path(source)
    is_xlsx = path.suffix.lower() == ".xlsx"
    if is_xlsx and not sheet:
        rows = table_to_expected.convert_xlsx_sheets(path, mapping or {}, amount_rules, skip_summary=True)
    else:
        name = sheet if is_xlsx else None
        if name == "审核汇总":
            raise ValueError("expected_rows_empty")
        data = table_to_expected.read_rows(path, name, target_url_header=(mapping or {}).get("target_url"))
        rows = table_to_expected.convert(data, mapping or {}, path.name, name, amount_rules)
    if not rows:
        raise ValueError("expected_rows_empty")
    ids = [row.get("case_id") for row in rows]
    if len(set(ids)) != len(ids) or not all(ids):
        raise ValueError("expected_case_ids_missing_or_duplicate")
    return {"rows": rows}


def review(expected: list[dict] | dict, observations: list[dict] | dict) -> dict[str, Any]:
    """Compare supplied evidence. Never navigate, relax duplicates, or invent evidence."""
    rows = review_comments.unwrap_rows(expected, "rows")
    captures = review_comments.unwrap_rows(observations, "observations")
    if not rows:
        raise ValueError("expected_rows_empty")
    report = review_comments.compare(rows, captures, amount_mode="expected-only", allow_duplicates=False)
    total = validate_results(rows, report["results"])
    report["schema_version"] = "1.0"
    report["summary"]["approved_amount"] = format(total, ".2f")
    return report


def audit_workbook(source: str | Path, observations: list[dict] | dict, output: str | Path,
                   *, mapping: dict[str, str] | None = None,
                   amount_rules: dict[str, str] | None = None) -> dict[str, Any]:
    """Convert + compare supplied observations + export; not a live platform capture."""
    from ._engine.build_audited_workbook import build_workbook

    expected = convert_table(source, mapping=mapping, amount_rules=amount_rules)
    report = review(expected, observations)
    report["workbook"] = build_workbook(Path(source), expected["rows"], report["results"], Path(output))
    return report
