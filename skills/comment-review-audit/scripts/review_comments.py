#!/usr/bin/env python3
"""Deterministic, read-only comparator for comment review batches."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


PLATFORM_ALIASES = {
    "douyin": "douyin",
    "抖音": "douyin",
    "xiaohongshu": "xiaohongshu",
    "xhs": "xiaohongshu",
    "小红书": "xiaohongshu",
}

TYPE_ALIASES = {
    "text": "text",
    "文字": "text",
    "image": "image",
    "图片": "image",
    "text+image": "text+image",
    "文字+图片": "text+image",
    "文字＋图片": "text+image",
    "text_image": "text+image",
}

PUNCTUATION = re.compile(
    r"[\s\u3000，。！？；：、‘’“”\"'（）()【】\[\]《》<>…·,!?;:/\\|~`@#$%^&*_+=-]"
)
XHS_EMOJI_TOKEN = re.compile(r"\[[^\[\]]+R\]", flags=re.IGNORECASE)
AMOUNT_TOKEN = re.compile(r"(?<![\d.])([+-]?\d+(?:\.\d+)?)(?:\s*)(万|千|百)?(?![\d.])")


def load_json(path: str) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def unwrap_rows(value: Any, key: str) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        rows = value
    elif isinstance(value, dict) and isinstance(value.get(key), list):
        rows = value[key]
    else:
        raise ValueError(f"JSON must be an array or an object containing '{key}'")
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"Every item in '{key}' must be an object")
    return rows


def canonical_platform(value: Any) -> Optional[str]:
    if value is None:
        return None
    return PLATFORM_ALIASES.get(str(value).strip().casefold())


def canonical_type(value: Any) -> Optional[str]:
    if value is None:
        return None
    raw = str(value).strip().casefold().replace(" ", "")
    return TYPE_ALIASES.get(raw)


def normalize_text(value: Any) -> str:
    raw = "" if value is None else str(value)
    normalized = unicodedata.normalize("NFKC", raw).casefold()
    normalized = XHS_EMOJI_TOKEN.sub("", normalized)
    # Treat a prose full stop like its Chinese counterpart, but retain decimal
    # points so materially different numbers never collapse (1.5 != 15).
    normalized = re.sub(r"(?<!\d)\.|\.(?!\d)", "", normalized)
    return PUNCTUATION.sub("", normalized)


def has_text(value: Any) -> bool:
    return bool(normalize_text(value))


def infer_type(row: Dict[str, Any]) -> Optional[str]:
    explicit = canonical_type(row.get("comment_type"))
    text_present = has_text(row.get("comment_text", row.get("text", "")))
    image_value = row.get("has_image", row.get("image", None))
    image_present = bool(image_value) if image_value is not None else False
    if explicit:
        if explicit == "text" and image_present:
            return None
        if explicit == "image" and text_present:
            return None
        if explicit == "text+image" and (not text_present or (image_value is not None and not image_present)):
            return None
        return explicit
    if text_present and image_present:
        return "text+image"
    if text_present:
        return "text"
    if image_present:
        return "image"
    return None


def image_signal(row: Dict[str, Any]) -> Optional[bool]:
    if "has_image" in row and isinstance(row.get("has_image"), bool):
        return row["has_image"]
    if "image" in row and isinstance(row.get("image"), bool):
        return row["image"]
    explicit = canonical_type(row.get("comment_type"))
    if explicit in {"image", "text+image"}:
        return True
    if explicit == "text":
        return False
    return None


def parse_amount(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value)).quantize(Decimal("0.01"))
        except InvalidOperation:
            return None
    raw = unicodedata.normalize("NFKC", str(value)).strip()
    if not raw:
        return None
    raw = raw.replace(",", "").replace("，", "")
    raw = re.sub(r"(?:人民币|元人民币|RMB|CNY|¥|￥|元|块)\s*", "", raw, flags=re.IGNORECASE)
    match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)(万|千|百)?", raw)
    if not match:
        return None
    try:
        number = Decimal(match.group(1))
        multiplier = {None: Decimal("1"), "百": Decimal("100"), "千": Decimal("1000"), "万": Decimal("10000")}[
            match.group(2)
        ]
        return (number * multiplier).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def extract_single_amount(text: Any) -> Tuple[Optional[Decimal], str]:
    raw = "" if text is None else unicodedata.normalize("NFKC", str(text))
    matches = list(AMOUNT_TOKEN.finditer(raw.replace(",", "").replace("，", "")))
    if len(matches) != 1:
        return None, "none" if not matches else "ambiguous"
    token = matches[0].group(1) + (matches[0].group(2) or "")
    return parse_amount(token), "single"


def observed_amount(comment: Dict[str, Any]) -> Tuple[Optional[Decimal], str]:
    explicit = parse_amount(comment.get("observed_amount"))
    if explicit is not None:
        return explicit, str(comment.get("amount_source") or "field")
    extracted, state = extract_single_amount(comment.get("text", ""))
    if extracted is not None:
        return extracted, "text_auto"
    return None, state


def target_key(row: Dict[str, Any]) -> str:
    value = row.get("target_key") or row.get("target_url")
    return "" if value is None else str(value).strip()


def platform_key(row: Dict[str, Any]) -> str:
    return canonical_platform(row.get("platform")) or str(row.get("platform", "")).strip().casefold()


def text_matches(expected: Dict[str, Any], comment: Dict[str, Any]) -> bool:
    expected_text = normalize_text(expected.get("comment_text", ""))
    actual_text = normalize_text(comment.get("text", comment.get("comment_text", "")))
    match_mode = str(expected.get("text_match", "exact")).strip().casefold()
    if not expected_text:
        return True
    if match_mode == "contains":
        return expected_text in actual_text
    return expected_text == actual_text


def prepare_observation(raw: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(raw)
    result["platform"] = canonical_platform(raw.get("platform")) or str(raw.get("platform", "")).strip().casefold()
    result["target_key"] = target_key(raw)
    result["comments"] = raw.get("comments") if isinstance(raw.get("comments"), list) else []
    return result


def compact_comment(comment: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("comment_id", "dom_index", "text", "comment_type", "has_image", "observed_amount", "amount_source", "evidence_path")
    return {key: comment[key] for key in keys if key in comment}


def format_amount(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else format(value, ".2f")


def compare(
    expected_rows: Iterable[Dict[str, Any]],
    observations: Iterable[Dict[str, Any]],
    *,
    amount_mode: str = "observed",
    allow_duplicates: bool = False,
) -> Dict[str, Any]:
    expected_rows = list(expected_rows)
    prepared_observations = [prepare_observation(item) for item in observations]
    by_target: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for item in prepared_observations:
        by_target.setdefault((item["platform"], item["target_key"]), []).append(item)

    combo_rows_by_target: Dict[Tuple[str, str], int] = {}
    for row in expected_rows:
        if infer_type({**row, "comment_text": row.get("comment_text", "")}) == "text+image":
            key = (platform_key(row), target_key(row))
            combo_rows_by_target[key] = combo_rows_by_target.get(key, 0) + 1

    results: List[Dict[str, Any]] = []
    for expected in expected_rows:
        result: Dict[str, Any] = {
            "case_id": expected.get("case_id"),
            "platform": platform_key(expected),
            "target_key": target_key(expected),
            "status": "needs_review",
            "reason_codes": [],
        }
        for provenance_key in ("source_file", "source_sheet", "source_row"):
            if provenance_key in expected:
                result[provenance_key] = expected[provenance_key]
        expected_type = infer_type({**expected, "comment_text": expected.get("comment_text", "")})
        result["expected_type"] = expected_type
        expected_amount = parse_amount(expected.get("expected_amount"))
        if not result["case_id"] or not result["platform"] or not result["target_key"]:
            result["reason_codes"].append("invalid_expected_identity")
            results.append(result)
            continue
        if expected_type is None:
            result["reason_codes"].append("invalid_or_contradictory_comment_type")
        if expected_amount is None:
            result["reason_codes"].append("invalid_expected_amount")
        if result["reason_codes"]:
            results.append(result)
            continue

        captures = by_target.get((result["platform"], result["target_key"]), [])
        if not captures:
            result["status"] = "blocked"
            result["reason_codes"].append("no_observation_for_target")
            results.append(result)
            continue

        usable = [
            item for item in captures
            if str(item.get("capture_status", "error")).casefold() == "ok"
        ]
        complete = [item for item in usable if item.get("capture_complete") is True]
        blockers = [item for item in captures if item not in usable]
        if not usable:
            result["status"] = "blocked"
            result["reason_codes"].append(str(blockers[0].get("capture_status", "blocked")))
            result["capture"] = {
                "capture_status": blockers[0].get("capture_status"),
                "evidence_path": blockers[0].get("evidence_path"),
                "warnings": blockers[0].get("warnings", []),
            }
            results.append(result)
            continue

        comments = [comment for item in usable for comment in item.get("comments", []) if isinstance(comment, dict)]
        image_detection_incomplete = any(item.get("image_detection_complete") is False for item in usable)
        same_text = [comment for comment in comments if text_matches(expected, comment)]
        if expected_type == "text+image":
            text_with_image = [comment for comment in same_text if image_signal(comment) is True]
            if same_text and not text_with_image:
                if any(image_signal(comment) is None for comment in same_text) or image_detection_incomplete:
                    result["status"] = "needs_review"
                    result["reason_codes"].append("comment_image_presence_unknown")
                    result["candidates"] = [compact_comment(comment) for comment in same_text]
                    results.append(result)
                    continue
                if not complete:
                    result["status"] = "blocked"
                    result["reason_codes"].append("capture_incomplete_image_not_seen")
                    result["candidates"] = [compact_comment(comment) for comment in same_text]
                    results.append(result)
                    continue
                result["status"] = "partial_text"
                result["reason_codes"].append("text_found_image_missing")
                result["candidates"] = [compact_comment(comment) for comment in same_text]
                result["expected_amount"] = format_amount(expected_amount)
                results.append(result)
                continue
            if not same_text:
                image_only = [
                    comment for comment in comments
                    if image_signal(comment) is True and not has_text(comment.get("text", ""))
                ]
                combo_count = combo_rows_by_target.get((result["platform"], result["target_key"]), 0)
                if len(image_only) == 1 and combo_count == 1:
                    if not complete:
                        result["status"] = "blocked"
                        result["reason_codes"].append("capture_incomplete_text_not_seen")
                        result["candidates"] = [compact_comment(image_only[0])]
                        results.append(result)
                        continue
                    result["status"] = "partial_image"
                    result["reason_codes"].append("image_found_text_missing")
                    result["candidates"] = [compact_comment(image_only[0])]
                    result["expected_amount"] = format_amount(expected_amount)
                    results.append(result)
                    continue
                if image_only:
                    result["status"] = "needs_review" if complete else "blocked"
                    result["reason_codes"].append(
                        "ambiguous_image_only_candidate" if complete else "capture_incomplete_ambiguous_image_only_candidate"
                    )
                    result["candidates"] = [compact_comment(comment) for comment in image_only]
                    results.append(result)
                    continue
                if image_detection_incomplete:
                    result["status"] = "needs_review"
                    result["reason_codes"].append("comment_image_presence_unknown")
                    results.append(result)
                    continue
        same_type = [
            comment
            for comment in same_text
            if infer_type({**comment, "comment_text": comment.get("text", "")}) == expected_type
        ]
        if not same_text:
            if complete:
                result["status"] = "not_found"
                result["reason_codes"].append("comment_text_not_found")
            else:
                result["status"] = "blocked"
                result["reason_codes"].append("capture_incomplete_comment_not_seen")
            results.append(result)
            continue
        if not same_type:
            if expected_type == "image" and image_detection_incomplete:
                result["status"] = "needs_review"
                result["reason_codes"].append("comment_image_presence_unknown")
                result["candidates"] = [compact_comment(comment) for comment in same_text if image_signal(comment) is None]
                results.append(result)
                continue
            result["status"] = "mismatch"
            result["reason_codes"].append("comment_type_mismatch")
            result["candidates"] = [compact_comment(comment) for comment in same_text]
            results.append(result)
            continue
        if len(same_type) > 1 and not allow_duplicates:
            result["status"] = "needs_review"
            result["reason_codes"].append("duplicate_matching_comments")
            result["candidates"] = [compact_comment(comment) for comment in same_type]
            results.append(result)
            continue

        match = same_type[0]
        if amount_mode == "expected-only":
            result["matched_comment"] = compact_comment(match)
            result["status"] = "verified"
            result["reason_codes"].append("comment_match_amount_from_table")
            result["expected_amount"] = format_amount(expected_amount)
            result["capture"] = {
                "captured_at": usable[0].get("captured_at"),
                "evidence_path": match.get("evidence_path") or usable[0].get("evidence_path"),
                "capture_complete": bool(complete),
            }
            results.append(result)
            continue
        actual_amount, amount_state = observed_amount(match)
        result["matched_comment"] = compact_comment(match)
        if actual_amount is None:
            result["status"] = "needs_review"
            result["reason_codes"].append("amount_ambiguous" if amount_state == "ambiguous" else "observed_amount_missing")
        elif actual_amount != expected_amount:
            result["status"] = "mismatch"
            result["reason_codes"].append("amount_mismatch")
            result["expected_amount"] = format_amount(expected_amount)
            result["observed_amount"] = format_amount(actual_amount)
        else:
            result["status"] = "verified"
            result["reason_codes"].append("comment_and_amount_match")
            result["expected_amount"] = format_amount(expected_amount)
            result["observed_amount"] = format_amount(actual_amount)
        result["capture"] = {
            "captured_at": usable[0].get("captured_at"),
            "evidence_path": match.get("evidence_path") or usable[0].get("evidence_path"),
            "capture_complete": bool(complete),
        }
        results.append(result)

    category_map = {
        "verified": "成功",
        "not_found": "失败",
        "mismatch": "失败",
        "needs_review": "待人审",
        "blocked": "待人审",
        "partial_text": "文字成功",
        "partial_image": "图片成功",
    }
    for result in results:
        if result.get("expected_type") == "text+image" and result["status"] == "verified":
            result["result_category"] = "图字成功"
        else:
            result["result_category"] = category_map[result["status"]]
        expected_row = next((row for row in expected_rows if row.get("case_id") == result.get("case_id")), {})
        component_amount = None
        if result["status"] == "partial_text":
            component_amount = parse_amount(expected_row.get("partial_text_amount"))
        elif result["status"] == "partial_image":
            component_amount = parse_amount(expected_row.get("partial_image_amount"))
        if result["status"] == "verified":
            result["triage"] = "成功"
            result["approved_amount"] = result.get("expected_amount")
        elif component_amount is not None:
            result["triage"] = "成功"
            result["approved_amount"] = format_amount(component_amount)
            result["reason_codes"].append("partial_component_amount_from_table")
        elif result["status"] in {"not_found", "mismatch"}:
            result["triage"] = "失败"
            result["approved_amount"] = "0.00"
        else:
            result["triage"] = "待人审"
            result["approved_amount"] = None

    statuses = ("verified", "not_found", "mismatch", "partial_text", "partial_image", "needs_review", "blocked")
    counts = {status: sum(1 for item in results if item["status"] == status) for status in statuses}
    triage_counts = {
        label: sum(1 for item in results if item["triage"] == label)
        for label in ("成功", "失败", "待人审")
    }
    result_category_counts = {
        label: sum(1 for item in results if item["result_category"] == label)
        for label in ("成功", "图字成功", "文字成功", "图片成功", "失败", "待人审")
    }
    return {
        "summary": {
            "total": len(results),
            "counts": counts,
            "triage_counts": triage_counts,
            "result_category_counts": result_category_counts,
        },
        "results": results,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", required=True, help="Expected-row JSON")
    parser.add_argument("--observations", required=True, help="Platform observation JSON")
    parser.add_argument("--output", help="Write report JSON to this path")
    parser.add_argument("--strict", action="store_true", help="Exit 2 if any row is not verified")
    parser.add_argument(
        "--amount-mode",
        choices=("observed", "expected-only"),
        default="observed",
        help="Use expected-only when the table amount is a payout rule, not text visible on the platform",
    )
    parser.add_argument(
        "--allow-duplicates",
        action="store_true",
        help="Treat one or more exact type-matching comments as an existence match",
    )
    args = parser.parse_args(argv)

    try:
        expected = unwrap_rows(load_json(args.expected), "rows")
        observations = unwrap_rows(load_json(args.observations), "observations")
        report = compare(
            expected,
            observations,
            amount_mode=args.amount_mode,
            allow_duplicates=args.allow_duplicates,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"input_error: {exc}", file=sys.stderr)
        return 1

    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)
    if args.strict and report["summary"]["counts"]["verified"] != report["summary"]["total"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
