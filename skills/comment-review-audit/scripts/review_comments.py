#!/usr/bin/env python3
"""Deterministic, read-only comparator for comment review batches."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, deque
from decimal import Decimal, InvalidOperation
from functools import cached_property
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
DISPLAY_FORMAT = re.compile("[\u200b\u2060\ufeff\ufe0e\ufe0f]")
POSSIBLE_REPLY_LABEL = re.compile(
    r"^(?:(?:(?:回复|回覆)\s*)?[@＠]\s*[^@＠\s:：]{1,64}(?:\s*[:：]\s*|\s+)"
    r"|(?:回复|回覆)\s*[^@＠\s:：]{1,64}\s*[:：]\s*)(?=\S)"
)
LEGACY_XHS_REVISIONS = {"xhs-replies-20261118-v4", "xhs-visible-end-v5"}
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
    # Ignore zero-width spacing and emoji presentation style, while preserving
    # the emoji itself, joined sequences, skin tone and numeric punctuation.
    normalized = DISPLAY_FORMAT.sub("", unicodedata.normalize("NFKC", raw).casefold())
    normalized = XHS_EMOJI_TOKEN.sub("", normalized)
    # Treat a prose full stop like its Chinese counterpart, but retain decimal
    # points so materially different numbers never collapse (1.5 != 15).
    normalized = re.sub(r"(?<!\d)\.|\.(?!\d)", "", normalized)
    return PUNCTUATION.sub("", normalized)


def has_text(value: Any) -> bool:
    return bool(normalize_text(value))


def infer_type(row: Dict[str, Any], *, text_present: Optional[bool] = None) -> Optional[str]:
    explicit = canonical_type(row.get("comment_type"))
    if text_present is None:
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
    extracted, state = extract_single_amount(_observed_body(comment))
    if extracted is not None:
        return extracted, "text_auto"
    return None, state


def target_key(row: Dict[str, Any]) -> str:
    value = row.get("target_key") or row.get("target_url")
    return "" if value is None else str(value).strip()


def platform_key(row: Dict[str, Any]) -> str:
    return canonical_platform(row.get("platform")) or str(row.get("platform", "")).strip().casefold()


def _observed_body(comment: Dict[str, Any]) -> Any:
    return comment.get("text", comment.get("comment_text", ""))


def _matching_text(comment: Dict[str, Any]) -> str:
    return normalize_text(_observed_body(comment))


def text_matches(expected: Dict[str, Any], comment: Dict[str, Any]) -> bool:
    expected_text = normalize_text(expected.get("comment_text", ""))
    actual_text = _matching_text(comment)
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
    keys = ("comment_id", "dom_index", "text", "comment_type", "has_image", "observed_amount", "amount_source",
            "evidence_path", "parent_comment_id", "parent_text", "parent_evidence", "reply_label_removed")
    record = {key: comment[key] for key in keys if key in comment}
    if "text" not in record and "comment_text" in comment:
        record["text"] = comment["comment_text"]
    if type(comment.get("thread_root_deleted")) is bool:
        record["thread_root_deleted"] = comment["thread_root_deleted"]
    return record


def format_amount(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else format(value, ".2f")


def _observed_type(comment: Dict[str, Any]) -> Optional[str]:
    return infer_type({**comment, "comment_text": _observed_body(comment)})


_REPLY_DIRECTIVE = re.compile(r"(?:第[一二三四五六七八九十0-9]+条)?回复(?:第([一二三四五六七八九十0-9]+)条|图片)?")


def _reply_number(value):
    """None is an ordinary source note; zero is a directive without a resolvable number."""
    if not value:
        return None
    match = _REPLY_DIRECTIVE.fullmatch(unicodedata.normalize("NFKC", str(value)).strip())
    if not match:
        return None
    number = match[1] or ""
    if number.isdecimal() and len(number) <= 6:
        return int(number)
    digits = "一二三四五六七八九"
    if number and len(number) == 1 and number in digits:
        return 1 + digits.index(number)
    chinese = re.fullmatch(r"([一二三四五六七八九]?)十([一二三四五六七八九]?)", number)
    if chinese:
        tens = digits.index(chinese[1]) + 1 if chinese[1] else 1
        ones = digits.index(chinese[2]) + 1 if chinese[2] else 0
        return tens * 10 + ones
    return 0


def _numbered_reply_parents(rows, indices, numbers, parents):
    try:
        from .table_to_expected import parse_task_ordinal
    except ImportError:  # Standalone adapter.
        from table_to_expected import parse_task_ordinal

    by_number = {}
    bodies = Counter(normalize_text(rows[i].get("comment_text")) for i in indices)
    for i in indices:
        ordinal = parse_task_ordinal(rows[i].get("task_ordinal"))
        if ordinal is None:
            return  # A missing label can conceal a second owner of any number.
        by_number[ordinal] = i if ordinal not in by_number else None
    for i in indices:
        if i not in numbers:
            continue
        candidate = by_number.get(numbers[i])
        if candidate is None or candidate == i or rows[candidate].get("reply_to"):
            continue
        body = normalize_text(rows[candidate].get("comment_text"))
        if body and bodies[body] == 1:
            parents[i] = body


def _reply_parents(rows):
    """Resolve worksheet ordinals inside one platform, target and source block."""
    numbers = {i: n for i, row in enumerate(rows) if (n := _reply_number(row.get("reply_to"))) is not None}
    if not numbers:
        return {}

    def group(row):
        return platform_key(row), target_key(row), str(row.get("source_file") or ""), str(row.get("source_sheet") or "")

    groups = {group(rows[i]): [] for i in numbers}
    for i, row in enumerate(rows):
        group_rows = groups.get(group(row))
        if group_rows is not None:
            group_rows.append(i)
    parents = dict.fromkeys(numbers)
    for indices in groups.values():
        positions = [rows[i].get("source_row") for i in indices]
        if any(n is not None for n in positions):
            if not all(type(n) is int for n in positions):
                continue
            indices = sorted(indices, key=lambda i: rows[i]["source_row"])
            positions = sorted(positions)
            if len(set(positions)) != len(positions):
                continue
        else:
            positions = indices
        if any("task_ordinal" in rows[i] for i in indices):
            # The same target can reappear later with its own numbering.
            start = 0
            for pos in range(1, len(positions)):
                if positions[pos] != positions[pos - 1] + 1:
                    _numbered_reply_parents(rows, indices[start:pos], numbers, parents)
                    start = pos
            _numbered_reply_parents(rows, indices[start:], numbers, parents)
            continue
        if rows[indices[0]].get("source_row") is not None and positions[-1] - positions[0] + 1 != len(positions):
            continue  # The positional convention is ambiguous across separate blocks.
        roots = [i for i in indices if i not in numbers]
        bodies = {i: normalize_text(rows[i].get("comment_text")) for i in indices}
        body_counts = Counter(bodies.values())
        for position, i in enumerate(indices):
            n = numbers.get(i, 0)
            if not 1 <= n <= len(indices):
                continue
            candidate = indices[n - 1]
            if candidate in numbers:
                continue
            if n <= len(roots):
                if roots[n - 1] != candidate:
                    continue  # Both numbering conventions are possible but disagree.
            elif n > position:
                continue  # Only use an all-row ordinal alone for an earlier parent.
            body = bodies[candidate]
            if body_counts[body] == 1:
                parents[i] = body
    return parents


def _reply_page_check(captures):
    """The last recorded same-target page attempt, or the latest explicit non-attempt."""
    selected = None
    for capture in reversed(captures):
        marker = capture.get("reply_verification")
        if not isinstance(marker, dict) or type(marker.get("page_attempted")) is not bool:
            continue
        if selected is None or marker["page_attempted"]:
            selected = capture, marker
        if marker["page_attempted"]:
            break
    audit = {"scope":"target", "page_attempted":None, "panel_available":None, "status":"not_recorded"}
    if selected:
        capture, marker = selected
        audit.update(page_attempted=marker["page_attempted"], panel_available=marker.get("panel_available"),
                     status=marker.get("status") or "recorded")
        for field in ("attempt_count", "structured_read", "unconfirmed_before", "unconfirmed_after"):
            if field in marker:
                audit[field] = marker[field]
        if capture.get("captured_at"):
            audit["captured_at"] = capture["captured_at"]
    return audit


def _identifier(value):
    return str(value).strip() if type(value) in (str, int) else ""


_ABSENT = object()


def _same_observed_fields(left, right, fields):
    for field in fields:
        a, b = left.get(field, _ABSENT), right.get(field, _ABSENT)
        if type(a) is not type(b) or a != b:
            return False
    return True


def _same_stable_record(left, right):
    # Amounts and relationship assertions must agree even across two adapters.
    if not _same_observed_fields(left, right, ("observed_amount", "amount_source", "parent_comment_id",
                                               "parent_text", "thread_root_deleted", "deleted")):
        return False
    a, b = _observed_body(left), _observed_body(right)
    if a != b and DISPLAY_FORMAT.sub("", str(a)) != DISPLAY_FORMAT.sub("", str(b)):
        return False
    if _same_observed_fields(left, right, ("comment_type", "has_image", "image")):
        return True
    # An explicit text type and has_image=False can describe the same row.
    return _observed_type(left) == _observed_type(right) and image_signal(left) == image_signal(right)


def _distinct_matches(comments):
    """Repeated snapshots can show the same stable ID, including at a new DOM index."""
    if len(comments) < 2:
        return comments
    first_by_id = {}
    distinct = []
    for comment in comments:
        cid = _identifier(comment.get("comment_id"))
        if cid and cid not in ("0", "-1"):
            position = first_by_id.get(cid)
            if position is not None:
                previous = distinct[position]
                if _same_stable_record(previous, comment):
                    if comment.get("parent_evidence") and not previous.get("parent_evidence"):
                        distinct[position] = comment
                    continue
            else:
                first_by_id[cid] = len(distinct)
        distinct.append(comment)
    return distinct


def _legacy_xhs(capture):
    return capture["platform"] == "xiaohongshu" and capture.get("parser_revision") in LEGACY_XHS_REVISIONS


def _observed_roots(evidence, parent_text):
    parents = []
    nested_ids = set()
    for record in evidence.by_text.get(parent_text, []):
        if record.get("deleted") or record.get("thread_root_deleted"):
            return []
        if _identifier(record.get("parent_comment_id")) or normalize_text(record.get("parent_text")):
            nested_ids.add(_identifier(record.get("comment_id")))
        else:
            parents.append(record)
    identities = {_identifier(p.get("comment_id")) or id(p) for p in parents}
    if len(identities) != 1:
        return []
    parent_id = _identifier(parents[0].get("comment_id"))
    if parent_id and parent_id in nested_ids:
        return []  # Contradictory placement for the same observed parent.
    return parents


def _other_displayed_parents(evidence, intended, candidates):
    intended_id = _identifier(intended.get("comment_id"))
    if len(evidence.usable) != 1 or intended_id in ("", "0", "-1"):
        return None
    records = evidence.by_comment_id
    if len(records.get(intended_id, ())) != 1:
        return None
    others = {}
    for comment in candidates:
        child_id = _identifier(comment.get("comment_id"))
        direct_id = _identifier(comment.get("parent_comment_id"))
        if (child_id in ("", "0", "-1") or direct_id in ("", "0", "-1", child_id, intended_id)
                or len(records.get(child_id, ())) != 1 or len(records.get(direct_id, ())) != 1
                or comment.get("deleted") or comment.get("thread_root_deleted")):
            return None
        other = records[direct_id][0]
        body = _matching_text(other)
        label = normalize_text(comment.get("parent_text"))
        if (not body or (label and label != body) or other.get("deleted") or other.get("thread_root_deleted")
                or _identifier(other.get("parent_comment_id")) or normalize_text(other.get("parent_text"))):
            return None
        others[direct_id] = {"comment_id": direct_id, "text": _observed_body(other)}
    return list(others.values())


def _reply_negative_capture(evidence, parent_text, candidates, expected_text):
    if not parent_text or not evidence.complete:
        return None
    # A rendered card can include "回复 @用户名" in the same span as the
    # message. A suffix is too weak for success but prevents claiming absence.
    if any(body.endswith(expected_text) and body != expected_text for body in evidence.addressed_texts):
        return None
    for parent in _observed_roots(evidence, parent_text):
        source = evidence.source(parent)
        if (source["platform"] == "xiaohongshu" and source.get("capture_complete") is True
                and source.get("capture_complete_source") == "visible_comment_list_end"
                and source.get("image_detection_complete") is True and not _legacy_xhs(source)):
            capture = {**_capture_details(parent, source), "capture_complete_source": "visible_comment_list_end",
                       "parent_text": _observed_body(parent)}
            if not candidates:
                return "not_found", "reply_text_absent_after_visible_end", capture
            others = _other_displayed_parents(evidence, parent, candidates)
            if others:
                capture.update(parent_comment_id=parent["comment_id"], observed_parents=others)
                return "mismatch", "reply_confirmed_under_other_parent", capture
    return None


def _matching_replies(evidence, parent_text, candidates):
    if not parent_text or not candidates:
        return []
    parents = _observed_roots(evidence, parent_text)
    if not parents:
        return []
    parent_id = _identifier(parents[0].get("comment_id"))
    parent_records = {id(p) for p in parents}
    captures_with_parent = {id(evidence.source(p)) for p in parents}
    replies = []
    contradicted = set()
    for comment in candidates:
        source = evidence.source(comment)
        direct_id = _identifier(comment.get("parent_comment_id"))
        direct_text = normalize_text(comment.get("parent_text"))
        child_id = _identifier(comment.get("comment_id"))
        if ((direct_id and direct_id != parent_id) or (direct_text and direct_text != parent_text)
                or ("parent_comment_id" in comment and not direct_id) or comment.get("deleted")
                or comment.get("thread_root_deleted")):
            if child_id not in ("", "0", "-1"):
                contradicted.add(child_id)
            continue
        if id(source) not in captures_with_parent or id(comment) in parent_records:
            continue
        if comment.get("parent_evidence") == "single_visible_reply_under_root":
            if source.get("capture_complete") is not True or not direct_text or comment.get("thread_root_deleted") is True:
                continue  # Page grouping requires a complete view and an intact, identified root.
        if parent_id and _identifier(comment.get("comment_id")) == parent_id:
            continue
        if (direct_id and direct_id == parent_id) or (direct_text and direct_text == parent_text):
            replies.append(comment)
    return [c for c in replies if _identifier(c.get("comment_id")) not in contradicted] if contradicted else replies


class _TargetEvidence:
    """Ordered captures for one target, with indexes built only when needed."""

    def __init__(self):
        self.captures = []
        self.row_count = 0
        self.last_row_index = None

    @cached_property
    def usable(self):
        return [c for c in self.captures if str(c.get("capture_status", "error")).casefold() == "ok"]

    @cached_property
    def complete(self):
        return any(c.get("capture_complete") is True and not _legacy_xhs(c) for c in self.usable)

    @cached_property
    def legacy_absence(self):
        return not self.complete and any(c.get("capture_complete") is True and _legacy_xhs(c) for c in self.usable)

    @cached_property
    def image_detection_incomplete(self):
        return any(c.get("image_detection_complete") is False for c in self.usable)

    @cached_property
    def comments(self):
        return [comment for c in self.usable for comment in c["comments"] if isinstance(comment, dict)]

    @cached_property
    def capture_by_record(self):
        sources = {}
        for capture in self.usable:
            for comment in capture["comments"]:
                if isinstance(comment, dict):
                    sources.setdefault(id(comment), capture)
        return sources

    def source(self, comment):
        if len(self.usable) == 1:
            return self.usable[0]
        return self.capture_by_record[id(comment)]

    @cached_property
    def normalized_texts(self):
        return [_matching_text(comment) for comment in self.comments]

    @cached_property
    def by_text(self):
        by_text = {}
        for text, comment in zip(self.normalized_texts, self.comments):
            by_text.setdefault(text, []).append(comment)
        return by_text

    @cached_property
    def addressed_texts(self):
        return [text for text, comment in zip(self.normalized_texts, self.comments)
                if str(_observed_body(comment)).lstrip().startswith(("回复", "回覆", "@", "＠"))]

    @cached_property
    def possible_reply_bodies(self):
        # A combined text node does not say whether the label is interface
        # text or part of the author's message. Keep the unmodified evidence.
        candidates = {}
        for comment in self.comments:
            if comment.get("deleted") is True or comment.get("reply_label_removed") is True:
                continue
            body = str(_observed_body(comment)).strip()
            if prefix := POSSIBLE_REPLY_LABEL.match(body):
                text = normalize_text(body[prefix.end():])
                candidates.setdefault(text, []).append(comment)
        return candidates

    @cached_property
    def by_comment_id(self):
        records = {}
        for comment in self.comments:
            records.setdefault(_identifier(comment.get("comment_id")), []).append(comment)
        return records

    @cached_property
    def by_type(self):
        by_type = {}
        for comment in self.comments:
            by_type.setdefault(_observed_type(comment), []).append(comment)
        return by_type

    @cached_property
    def image_only(self):
        return [comment for comment in self.comments
                if image_signal(comment) is True and not has_text(_observed_body(comment))]

    def matching(self, text: str, mode: str):
        if not text:
            return self.comments
        if self.row_count == 1:
            # A one-off requirement never reuses a normalized-text index.
            if mode == "contains":
                return [c for c in self.comments if text in _matching_text(c)]
            return [c for c in self.comments if text == _matching_text(c)]
        if mode == "contains":
            # The same normalized text can recur after other matching comments.
            return [c for actual, c in zip(self.normalized_texts, self.comments) if text in actual]
        return self.by_text.get(text, [])


def _claim_key(result, comment):
    # DOM positions and equal text cannot identify a comment across captures.
    identifier = comment.get("comment_id")
    if type(identifier) in (str, int) and str(identifier).strip():
        return result["platform"], result["target_key"], "id", str(identifier).strip()
    return result["platform"], result["target_key"], "record", id(comment)


def _capture_details(comment, source):
    return {"captured_at": source.get("captured_at"),
            "evidence_path": comment.get("evidence_path") or source.get("evidence_path"),
            "capture_complete": source.get("capture_complete") is True}


def _assign_distinct_comment(row, choices, owners, assigned):
    """Find an unused eligible comment, moving earlier flexible matches if needed."""
    queue = deque([row])
    previous = {row: None}
    while queue:
        current = queue.popleft()
        for key in choices[current]:
            owner = owners.get(key)
            if owner is None:
                while current is not None:
                    old_key = assigned.get(current)
                    owners[key] = current
                    assigned[current] = key
                    current, key = previous[current], old_key
                return set()
            if owner not in previous:
                queue.append(owner)
                previous[owner] = current
    # All reachable evidence is spoken for; row order cannot settle ownership.
    return set(previous)


def _check_settlement_claims(results, claims, amount_mode, allow_duplicates, note_presence=False):
    owners, assigned, choices = {}, {}, {}
    disputed = set()
    # An unidentified card can be the same card seen in a different snapshot.
    # Share one settlement identity for its body across such views, preferring
    # an available stable ID. This never blocks the visibility verdict.
    anonymous_keys = {}
    if note_presence:
        for index, (comments, _) in claims.items():
            result = results[index]
            for comment in comments:
                if _identifier(comment.get("comment_id")) not in ("", "0", "-1"):
                    body = (result["platform"], result["target_key"], _matching_text(comment))
                    anonymous_keys.setdefault(body, _claim_key(result, comment))
    for index, (comments, _) in claims.items():
        result = results[index]
        if result["triage"] != "成功":
            continue
        if not allow_duplicates and not note_presence:
            for comment in comments:
                owner = owners.setdefault(_claim_key(result, comment), index)
                if owner != index:
                    disputed.update((owner, index))
            continue
        eligible = {}
        required_amount = (Decimal(result["expected_amount"])
                           if amount_mode == "observed" and result["status"] == "verified" else None)
        for position, comment in enumerate(comments):
            if position and required_amount is not None:
                amount, _ = observed_amount(comment)
                if amount != required_amount:
                    continue
            key = _claim_key(result, comment)
            if note_presence and _identifier(comment.get("comment_id")) in ("", "0", "-1"):
                body = (result["platform"], result["target_key"], _matching_text(comment))
                key = anonymous_keys.get(body, (body[0], body[1], "unidentified_body", body[2]))
            eligible.setdefault(key, comment)
        choices[index] = eligible
        disputed.update(_assign_distinct_comment(index, choices, owners, assigned))

    for index, key in assigned.items():
        comments, sources = claims[index]
        chosen = choices[index][key]
        if index not in disputed and chosen is not comments[0]:
            result = results[index]
            result["matched_comment"] = compact_comment(chosen)
            result["capture"] = _capture_details(chosen, sources[id(chosen)])
    for index in disputed:
        result = results[index]
        if note_presence:
            result["settlement_status"] = "pending_duplicate_claim"
            result["approved_amount"] = None
            result["reason_codes"].append("evidence_claimed_by_multiple_rows")
            continue
        if "matched_comment" in result:
            result["candidates"] = [compact_comment(c) for c in claims[index][0]]
            del result["matched_comment"]
        result.update(status="needs_review", reason_codes=["evidence_claimed_by_multiple_rows"],
                      result_category="待人审", triage="待人审", approved_amount=None)


def compare(
    expected_rows: Iterable[Dict[str, Any]],
    observations: Iterable[Dict[str, Any]],
    *,
    amount_mode: str = "observed",
    allow_duplicates: bool = False,
    require_reply_parent: bool = False,
    reply_context_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    expected_rows = list(expected_rows)
    note_presence = not require_reply_parent and amount_mode == "expected-only"
    expected_by_case = {}
    for row in expected_rows:
        expected_by_case.setdefault(row.get("case_id"), row)
    by_target: Dict[Tuple[str, str], _TargetEvidence] = {}
    for raw in observations:
        item = prepare_observation(raw)
        key = (item["platform"], item["target_key"])
        if key not in by_target:
            by_target[key] = _TargetEvidence()
        by_target[key].captures.append(item)

    combo_rows_by_target: Dict[Tuple[str, str], int] = {}
    expected_signals = []
    for index, row in enumerate(expected_rows):
        key = (platform_key(row), target_key(row))
        evidence = by_target.get(key)
        if evidence is not None:
            evidence.row_count += 1
            evidence.last_row_index = index
        text = normalize_text(row.get("comment_text", ""))
        expected_type = infer_type(row, text_present=bool(text))
        expected_signals.append((text, expected_type))
        if expected_type == "text+image":
            combo_rows_by_target[key] = combo_rows_by_target.get(key, 0) + 1
    if reply_context_rows is not None and combo_rows_by_target:
        # A bare image on a mixed note still has all the original combo tasks as possible owners.
        for source in reply_context_rows:
            key = (platform_key(source), target_key(source))
            if key not in combo_rows_by_target or source["case_id"] in expected_by_case:
                continue
            if infer_type(source, text_present=bool(normalize_text(source.get("comment_text", "")))) == "text+image":
                combo_rows_by_target[key] += 1

    results: List[Dict[str, Any]] = []
    settlement_claims = {}
    legacy_absence_targets = set()
    reply_parents = {}
    if require_reply_parent and reply_context_rows is not None:
        # A carried-forward row can still identify the source parent of an active reply.
        by_case = {reply_context_rows[i]["case_id"]: parent for i, parent in _reply_parents(reply_context_rows).items()}
        reply_parents = {i: by_case[row["case_id"]] for i, row in enumerate(expected_rows) if row["case_id"] in by_case}
    elif require_reply_parent:
        reply_parents = _reply_parents(expected_rows)
    reply_checks = {}
    for index, expected in enumerate(expected_rows):
        key = (platform_key(expected), target_key(expected))
        # Release large text indexes after the last row for their target.
        evidence = by_target.get(key)
        if index in reply_parents and key not in reply_checks:
            reply_checks[key] = _reply_page_check(evidence.captures if evidence else [])
        if evidence is not None and index == evidence.last_row_index:
            del by_target[key]
        result: Dict[str, Any] = {
            "case_id": expected.get("case_id"),
            "platform": key[0],
            "target_key": key[1],
            "status": "needs_review",
            "reason_codes": [],
        }
        for provenance_key in ("source_file", "source_sheet", "source_row"):
            if provenance_key in expected:
                result[provenance_key] = expected[provenance_key]
        expected_text, expected_type = expected_signals[index]
        if expected_type == "text" and not expected_text:
            expected_type = None
        result["expected_type"] = expected_type
        expected_amount = parse_amount(expected.get("expected_amount"))
        if not result["case_id"] or not result["platform"] or not result["target_key"]:
            result["reason_codes"].append("invalid_expected_identity")
            results.append(result)
            continue
        if expected_type is None:
            result["reason_codes"].append("invalid_or_contradictory_comment_type")
        if note_presence and expected_amount is not None and (not expected_amount.is_finite() or expected_amount < 0):
            expected_amount = None
        if expected_amount is None and not note_presence:
            result["reason_codes"].append("invalid_expected_amount")
        if result["reason_codes"]:
            results.append(result)
            continue

        if evidence is None:
            result["status"] = "blocked"
            result["reason_codes"].append("no_observation_for_target")
            results.append(result)
            continue

        usable = evidence.usable
        if not usable:
            first = evidence.captures[0]
            result["status"] = "blocked"
            result["reason_codes"].append(str(first.get("capture_status", "blocked")))
            result["capture"] = {
                "capture_status": first.get("capture_status"),
                "evidence_path": first.get("evidence_path"),
                "warnings": first.get("warnings", []),
            }
            results.append(result)
            continue

        complete = evidence.complete
        if evidence.legacy_absence:
            legacy_absence_targets.add(key)
        image_detection_incomplete = evidence.image_detection_incomplete
        match_mode = str(expected.get("text_match", "exact")).strip().casefold()
        same_text = evidence.matching(expected_text, match_mode)
        if not require_reply_parent:
            same_text = [c for c in same_text if c.get("deleted") is not True]
        if note_presence and expected_text and not same_text and match_mode == "exact":
            ambiguous = evidence.possible_reply_bodies.get(expected_text)
            if ambiguous:
                result["reason_codes"].append("reply_body_prefix_unconfirmed")
                result["candidates"] = [compact_comment(c) for c in ambiguous]
                results.append(result)
                continue
        allow_text_alternatives = allow_duplicates or (note_presence and bool(expected_text))
        constrained_reply = index in reply_parents
        reply_images = []
        if constrained_reply:
            parent_text = reply_parents[index]
            possible = same_text if expected_text else evidence.by_type.get(expected_type, [])
            same_text = _matching_replies(evidence, parent_text, possible)
            if not same_text and expected_type == "text+image":
                reply_images = _matching_replies(evidence, parent_text, evidence.image_only)
            if not same_text and not reply_images:
                decision = _reply_negative_capture(evidence, parent_text, possible, expected_text) if expected_type == "text" else None
                if decision:
                    status, reason, capture = decision
                    result.update(status=status, reason_codes=[reason], capture=capture)
                else:
                    result["reason_codes"].append("reply_parent_unconfirmed" if parent_text else "reply_source_parent_ambiguous")
                if possible:
                    result["candidates"] = [compact_comment(c) for c in possible]
                results.append(result)
                continue
        if expected_type == "text+image":
            text_with_image = [comment for comment in same_text if image_signal(comment) is True]
            if same_text and not text_with_image:
                same_text = _distinct_matches(same_text)
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
                if len(same_text) > 1 and not allow_text_alternatives:
                    result["reason_codes"].append("duplicate_matching_comments")
                    result["candidates"] = [compact_comment(comment) for comment in same_text]
                    results.append(result)
                    continue
                result["status"] = "partial_text"
                result["reason_codes"].append("text_found_image_missing")
                result["candidates"] = [compact_comment(comment) for comment in same_text]
                result["expected_amount"] = format_amount(expected_amount)
                if evidence.row_count > 1:
                    sources = evidence.capture_by_record if len(same_text) > 1 else None
                    settlement_claims[index] = same_text, sources
                results.append(result)
                continue
            if not same_text:
                image_only = _distinct_matches(reply_images if constrained_reply else
                                               [c for c in evidence.image_only if c.get("deleted") is not True])
                combo_count = combo_rows_by_target.get(key, 0)
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
                    if evidence.row_count > 1:
                        settlement_claims[index] = image_only, None
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
        if note_presence and expected_type == "text":
            same_type = same_text
        elif expected_text or constrained_reply:
            same_type = [comment for comment in same_text if _observed_type(comment) == expected_type]
        else:
            same_type = [c for c in evidence.by_type.get(expected_type, []) if c.get("deleted") is not True]
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
            result["status"] = "mismatch" if complete else "blocked"
            result["reason_codes"].append("comment_type_mismatch" if complete else "capture_incomplete_required_type_not_seen")
            result["candidates"] = [compact_comment(comment) for comment in same_text]
            results.append(result)
            continue
        same_type = _distinct_matches(same_type)
        if len(same_type) > 1 and not allow_text_alternatives:
            result["status"] = "needs_review"
            result["reason_codes"].append("duplicate_matching_comments")
            result["candidates"] = [compact_comment(comment) for comment in same_type]
            results.append(result)
            continue

        match = same_type[0]
        if evidence.row_count > 1:
            sources = evidence.capture_by_record if len(same_type) > 1 else None
            settlement_claims[index] = same_type, sources
        if amount_mode == "expected-only":
            result["matched_comment"] = compact_comment(match)
            result["status"] = "verified"
            result["reason_codes"].append("comment_match_amount_from_table" if expected_amount is not None else "invalid_expected_amount")
            result["expected_amount"] = format_amount(expected_amount)
            result["capture"] = _capture_details(match, evidence.source(match))
            results.append(result)
            continue
        actual_amount, amount_state = observed_amount(match)
        result["matched_comment"] = compact_comment(match)
        if actual_amount is None:
            result["status"] = "needs_review"
            result["reason_codes"].append("amount_ambiguous" if amount_state == "ambiguous" else "observed_amount_missing")
        elif actual_amount != expected_amount:
            result["status"] = "mismatch" if complete else "blocked"
            result["reason_codes"].append("amount_mismatch" if complete else "capture_incomplete_required_amount_not_seen")
            result["expected_amount"] = format_amount(expected_amount)
            result["observed_amount"] = format_amount(actual_amount)
        else:
            result["status"] = "verified"
            result["reason_codes"].append("comment_and_amount_match")
            result["expected_amount"] = format_amount(expected_amount)
            result["observed_amount"] = format_amount(actual_amount)
        result["capture"] = _capture_details(match, evidence.source(match))
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
        if ((result["platform"], result["target_key"]) in legacy_absence_targets
                and any(code.startswith("capture_incomplete_") or code == "reply_parent_unconfirmed"
                        for code in result["reason_codes"])):
            result["reason_codes"].append("legacy_xhs_visible_recheck_required")
        if result.get("expected_type") == "text+image" and result["status"] == "verified":
            result["result_category"] = "图字成功"
        else:
            result["result_category"] = category_map[result["status"]]
        expected_row = expected_by_case.get(result.get("case_id"), {})
        component_amount = None
        if result["status"] == "partial_text":
            component_amount = parse_amount(expected_row.get("partial_text_amount"))
        elif result["status"] == "partial_image":
            component_amount = parse_amount(expected_row.get("partial_image_amount"))
        if result["status"] == "verified":
            result["triage"] = "成功"
            result["approved_amount"] = result.get("expected_amount")
            if note_presence and result["approved_amount"] is None:
                result["settlement_status"] = "pending_source_amount"
        elif note_presence and result["status"] in ("partial_text", "partial_image") and (
                component_amount is None or not component_amount.is_finite() or component_amount < 0):
            result["triage"] = "成功"
            result["approved_amount"] = None
            result["settlement_status"] = "pending_source_amount"
            result["reason_codes"].append("missing_component_amount")
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

    _check_settlement_claims(results, settlement_claims, amount_mode, allow_duplicates, note_presence)
    for index, parent in reply_parents.items():
        result = results[index]
        if result["triage"] == "待人审":
            reasons = result["reason_codes"]
            if not parent and "reply_source_parent_ambiguous" not in reasons:
                reasons.append("reply_source_parent_ambiguous")
            key = result["platform"], result["target_key"]
            primary = next((reason for reason in reasons if reason in ("reply_parent_unconfirmed", "reply_source_parent_ambiguous")),
                           reasons[0])
            check = {**reply_checks[key], "source_parent_resolved": bool(parent), "reason": primary}
            candidates = result.get("candidates", [])
            if not candidates and "matched_comment" in result:
                candidates = [result["matched_comment"]]
            expected = expected_rows[index]
            if has_text(expected.get("comment_text", "")):
                deleted_matches = sum(c.get("thread_root_deleted") is True and text_matches(expected, c)
                                      for c in candidates)
                if deleted_matches:
                    # A thread root and a direct parent can be different comments.
                    reasons.append("reply_thread_root_deleted")
                    check["matching_candidates_with_deleted_root"] = deleted_matches
            check["reason_codes"] = list(reasons)
            result["reply_verification"] = check
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
            "settlement_pending_count": sum(bool(r.get("settlement_status")) for r in results),
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
    parser.add_argument("--require-reply-parent", action="store_true",
                        help="Also verify the directly requested parent comment for numbered reply tasks")
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
        if any("prior_review" in r for r in expected):
            if __package__:
                from .audit_pass import compare_audit_rows
            else:
                from audit_pass import compare_audit_rows
            report = compare_audit_rows(expected, observations, require_reply_parent=args.require_reply_parent,
                                        amount_mode=args.amount_mode, allow_duplicates=args.allow_duplicates)
        else:
            report = compare(expected, observations, amount_mode=args.amount_mode,
                             allow_duplicates=args.allow_duplicates, require_reply_parent=args.require_reply_parent)
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
    reviewed = report["summary"].get("audit_pass", {}).get("reviewed_rows", report["summary"]["total"])
    if args.strict and report["summary"]["counts"]["verified"] != reviewed:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
