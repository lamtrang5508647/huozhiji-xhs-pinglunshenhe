"""Reuse only fresh, complete evidence from the same parser and target."""
from datetime import datetime, timezone
import hashlib
import json

# Bump the relevant revision when comment extraction or completeness changes.
PARSER_REVISIONS = {'xiaohongshu':'xhs-visible-controls-v7', 'douyin':'dy-body-20260930-v2'}

def recheck_scope(rows):
    """Identify a success-only capture, including changes to the incoming first-pass results."""
    if not any("prior_review" in row for row in rows):
        return None
    encoded = json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

def fresh_capture(item, platform, key, *, now=None):
    if not isinstance(item, dict):
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
