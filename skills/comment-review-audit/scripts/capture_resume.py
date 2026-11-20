"""Reuse only fresh, complete evidence from the same parser and target."""
from datetime import datetime, timezone
import hashlib
import json

EMPTY_STATE_SOURCE = "visible_comment_empty_state"
EMPTY_STATE_LABEL = "barren_land_click_to_comment"


def consistent_empty_state(item):
    if item.get("capture_complete_source") != EMPTY_STATE_SOURCE:
        return True
    return (item.get("platform") == "xiaohongshu" and item.get("empty_state_label") == EMPTY_STATE_LABEL
            and item.get("target_page_state") == "target_note" and item.get("capture_status") == "ok"
            and item.get("visible_comment_panel_readable") is True and item.get("capture_complete") is True
            and item.get("image_detection_complete") is True and item.get("comments") == []
            and type(item.get("captured_comment_count")) is int and item["captured_comment_count"] == 0)

# Bump the relevant revision when comment extraction or completeness changes.
PARSER_REVISIONS = {'xiaohongshu':'xhs-visible-evidence-v8', 'douyin':'dy-visible-replies-v3'}

def recheck_scope(rows):
    """Identify a success-only capture, including changes to the incoming first-pass results."""
    if not any("prior_review" in row for row in rows):
        return None
    encoded = json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

def fresh_capture(item, platform, key, *, now=None):
    if not isinstance(item, dict) or not consistent_empty_state(item):
        return False
    if (item.get('platform') != platform or str(item.get('target_key')) != str(key)
            or item.get('capture_status') != 'ok'
            or item.get('image_detection_complete') is False
            or item.get('parser_revision') != PARSER_REVISIONS.get(platform)):
        return False
    comments = item.get('comments')
    if not isinstance(comments, list) or any(
            not isinstance(c, dict) or not isinstance(c.get('has_image'), bool) for c in comments):
        return False
    try:
        captured = datetime.fromisoformat(item['captured_at'].replace('Z', '+00:00'))
        age = ((now or datetime.now(timezone.utc)) - captured).total_seconds()
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    return 0 <= age <= 24 * 3600

def reusable_capture(item, platform, key, *, now=None):
    return fresh_capture(item, platform, key, now=now) and item.get('capture_complete') is True

def pending_targets(targets, existing, platform, key_field, *, refresh=False):
    return [t for t in targets if refresh or not reusable_capture(
        existing.get(str(t[key_field])), platform, t[key_field])]
