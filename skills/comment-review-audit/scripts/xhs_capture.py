#!/usr/bin/env python3
"""Safely capture one Xiaohongshu note's comments into the observation contract."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from runtime_environment import child_environment, configure_stdio, process_group_options, terminate_process_tree


def first_value(item: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def text_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        nested = first_value(value, ("text", "content", "desc", "title"))
        return text_value(nested) if nested is not None else ""
    return "" if value is None else str(value)


def has_image(item: Dict[str, Any]) -> bool:
    value = first_value(item, ("image_list", "imageList", "images", "pictures", "image", "image_info", "media"))
    return bool(value)


def clean_comment_id(value: Any) -> Optional[str]:
    if type(value) not in (int, str):
        return None
    value = str(value).strip()
    return value if value and value not in ("0", "-1") else None


def deleted_placeholder(text: str) -> bool:
    return text.strip().strip("[]【】") in ("原评论已删除", "该评论已删除", "评论已被删除", "该评论已被删除")


def add_reply_relation(result: Dict[str, Any], raw: Dict[str, Any], *, root_list: bool) -> None:
    direct_keys = ("parent_comment_id", "parentCommentId", "target_comment_id", "targetCommentId")
    object_keys = ("target_comment", "targetComment", "parent_comment", "parentComment")
    identifiers = set()
    parent_bodies = set()
    for key in direct_keys:
        identifier = clean_comment_id(raw.get(key))
        if identifier:
            identifiers.add(identifier)
    for key in object_keys:
        target = raw.get(key)
        if isinstance(target, dict):
            identifier = clean_comment_id(first_value(target, ("id", "comment_id", "commentId", "cid")))
            if identifier:
                identifiers.add(identifier)
            if target.get("is_deleted") is True or target.get("isDeleted") is True:
                return
            for field in ("text", "content", "comment_text", "commentText"):
                body = target.get(field)
                if isinstance(body, str) and body.strip():
                    if deleted_placeholder(body):
                        return
                    parent_bodies.add(body.strip())
    if len(identifiers) > 1 or result.get("comment_id") in identifiers or len(parent_bodies) > 1:
        return
    if parent_bodies:
        # A direct target object can explicitly quote the parent without exposing its ID.
        result["parent_text"] = parent_bodies.pop()
    if identifiers:
        result["parent_comment_id"] = identifiers.pop()
    elif root_list:
        # Only classify the outer list of a known note-comments response as
        # roots. A root/thread hint or unidentifiable target may mark a reply.
        hints = (*direct_keys, *object_keys, "root_comment_id", "rootCommentId")
        if not any(raw.get(key) not in (None, "", {}, 0, "0") for key in hints):
            result["parent_comment_id"] = None


def normalize_comment(item: Dict[str, Any], *, root_list: bool = False) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    comment_id = clean_comment_id(first_value(item, ("comment_id", "commentId", "id", "cid")))
    text = text_value(first_value(item, ("text", "content", "comment_text", "commentText", "desc")))
    if comment_id is not None:
        result["comment_id"] = str(comment_id)
    result["text"] = text
    result["has_image"] = has_image(item)
    observed_amount = first_value(item, ("observed_amount", "amount", "money", "price"))
    if isinstance(observed_amount, (str, int, float)) and not isinstance(observed_amount, bool):
        result["observed_amount"] = observed_amount
        result["amount_source"] = "field"
    add_reply_relation(result, item, root_list=root_list)
    if deleted_placeholder(text) or item.get("is_deleted") is True or item.get("isDeleted") is True:
        result.update(text="", has_image=False, deleted=True)
    return result


def _walk_comments(items: Iterable[Dict[str, Any]], outer=True):
    for item in items:
        if not isinstance(item, dict):
            continue
        yield item, outer
        for key in ("subComments", "sub_comments", "replies", "children"):
            nested = item.get(key)
            if isinstance(nested, list):
                yield from _walk_comments(nested, False)


def flatten_comments(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [item for item, _ in _walk_comments(items)]


def normalized_comments(items: Iterable[Dict[str, Any]], *, root_list: bool = False) -> List[Dict[str, Any]]:
    """Normalize a note's comments while retaining only confirmed immediate reply targets."""
    # A nested list can contain replies to other replies within the same thread.
    return [normalize_comment(item, root_list=root_list and outer) for item, outer in _walk_comments(items)]


def find_comments(value: Any) -> Optional[List[Dict[str, Any]]]:
    preferred_keys = ("comments", "comment_list", "commentList", "commentData")
    if isinstance(value, dict):
        for key in preferred_keys:
            nested = value.get(key)
            if isinstance(nested, list) and all(isinstance(item, dict) for item in nested):
                return nested
        for nested in value.values():
            found = find_comments(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value):
            if any(first_value(item, ("comment_id", "commentId", "comment_text", "commentText", "content", "text")) is not None for item in value):
                return value
        for nested in value:
            found = find_comments(nested)
            if found is not None:
                return found
    return None


PAGE_FLAGS = ("has_more", "hasMore", "more", "has_next", "hasNext")
REPLY_FLAGS = ("sub_comment_has_more", "subCommentHasMore", "has_more_sub_comments", "hasMoreSubComments")


def has_more(value: Any) -> bool:
    if isinstance(value, dict):
        for key in (*PAGE_FLAGS, *REPLY_FLAGS):
            if str(value.get(key)).lower() in ("true", "1"):
                return True
        count = first_value(value, ("sub_comment_count", "subCommentCount", "reply_count", "replyCount"))
        if str(count).isdigit() and int(count) > 0:
            children = first_value(value, ("subComments", "sub_comments", "replies", "children"))
            if not isinstance(children, list) or len(children) < int(count):
                return True
        return any(has_more(nested) for nested in value.values())
    if isinstance(value, list):
        return any(has_more(nested) for nested in value)
    return False


def comments_exhausted(payload, comments):
    """A bare list or missing pagination fields cannot establish a negative."""
    if not isinstance(payload, dict):
        return False
    if any(payload.get(k) is comments for k in ("comments", "comment_list", "commentList", "commentData")):
        return (any(str(payload.get(k)).lower() in ("false", "0") for k in PAGE_FLAGS)
                and not has_more(payload))
    return any(comments_exhausted(value, comments) for value in payload.values() if isinstance(value, dict))


def decode_json_output(stdout: str) -> Any:
    decoder = json.JSONDecoder()
    for index, char in enumerate(stdout):
        if char not in "[{":
            continue
        try:
            payload, _ = decoder.raw_decode(stdout[index:])
            return payload
        except json.JSONDecodeError:
            continue
    raise ValueError("xhs_output_not_json")


def classify_failure(returncode: int, stderr: str) -> str:
    text = stderr.casefold()
    if any(marker in text for marker in ("captcha", "验证码")):
        return "captcha"
    if any(
        marker in text
        for marker in (
            "risk",
            "风控",
            "频繁",
            "security",
            "安全限制",
            "ip存在风险",
            "300012",
            "website-login/error",
        )
    ):
        return "risk_control"
    if any(marker in text for marker in ("login", "登录", "cookie", "未登录")):
        return "session_expired"
    return "error"


def blocked_observation(note_id: str, status: str, reason: str) -> Dict[str, Any]:
    return {
        "platform": "xiaohongshu",
        "target_key": note_id,
        "capture_status": status,
        "capture_complete": False,
        "warnings": [reason],
        "comments": [],
    }


def capture(note_id: str, timeout: float, evidence_path: Optional[str] = None) -> Dict[str, Any]:
    command = ["xhs", "read", note_id, "--comments", "--json"]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        env=child_environment(),
        **process_group_options(),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_process_tree(process)
        result = blocked_observation(note_id, "error", "adapter_timeout")
        if evidence_path:
            result["evidence_path"] = evidence_path
        return result
    if process.returncode != 0:
        result = blocked_observation(
            note_id,
            classify_failure(process.returncode, stdout + "\n" + stderr),
            "xhs_command_failed",
        )
        if evidence_path:
            result["evidence_path"] = evidence_path
        return result
    try:
        payload = decode_json_output(stdout)
        raw_comments = find_comments(payload)
        if raw_comments is None:
            raise ValueError("comments_not_found_in_payload")
    except (ValueError, json.JSONDecodeError):
        result = blocked_observation(note_id, "error", "xhs_payload_unusable")
        if evidence_path:
            result["evidence_path"] = evidence_path
        return result
    result: Dict[str, Any] = {
        "platform": "xiaohongshu",
        "target_key": note_id,
        "capture_status": "ok",
        "capture_complete": comments_exhausted(payload, raw_comments),
        "comments": normalized_comments(raw_comments, root_list=isinstance(payload, dict)
                                        and payload.get("comments") is raw_comments),
    }
    if evidence_path:
        result["evidence_path"] = evidence_path
    if result["capture_complete"]:
        result["capture_complete_source"] = "explicit_comment_pagination_end"
    if not result["capture_complete"]:
        result["warnings"] = ["comments_pagination_incomplete"]
    return result


def main(argv: Optional[List[str]] = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--note-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--evidence-path")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        result = capture(args.note_id, args.timeout, args.evidence_path)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"observations": [result]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"capture_status={result['capture_status']} target={args.note_id}")
        return 0
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        print(f"adapter_error: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
