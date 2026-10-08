#!/usr/bin/env python3
"""Capture Douyin comments from expected-row JSON using a persistent visible Chrome profile."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
except ImportError:
    PlaywrightTimeoutError = TimeoutError  # parser-only hosts never call a live browser
from capture_pacing import has_remaining_target, pause_seconds
from capture_resume import pending_targets, PARSER_REVISIONS
from profile_lock import ProfileLock, default_profile
from runtime_environment import configure_stdio
from session_environment import network_fingerprint
from browser_environment import find_chrome


CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
TIME_LINE = re.compile(r"^(?:刚刚|\d+\s*(?:秒钟|分钟|小时|天|周|月|年)前|\d{1,2}[-/.]\d{1,2})(?:·.*)?$")


def sync_playwright():
    from playwright.sync_api import sync_playwright as start
    return start()


def atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def expected_rows(path: str) -> Iterable[Dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("rows", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError(f"expected rows are not a list: {path}")
    return rows


def targets_from_expected(paths: Iterable[str]) -> List[Dict[str, Any]]:
    targets: Dict[str, Dict[str, Any]] = {}
    for input_path in paths:
        for row in expected_rows(input_path):
            if row.get("platform") != "douyin":
                continue
            key = str(row.get("target_key") or "").strip()
            url = str(row.get("target_url") or "").strip()
            if not key or not url:
                continue
            target = targets.setdefault(key, {"target_key": key, "url": url, "source_refs": []})
            ref = {
                "file": row.get("source_file"),
                "sheet": row.get("source_sheet"),
                "row": row.get("source_row"),
            }
            if ref not in target["source_refs"]:
                target["source_refs"].append(ref)
    return list(targets.values())


def visible_marker(page: Any, markers: Iterable[str]) -> bool:
    for marker in markers:
        try:
            locator = page.get_by_text(marker, exact=False)
            if locator.count() and locator.first.is_visible():
                return True
        except Exception:
            continue
    return False


def security_status(page: Any) -> str:
    url = str(page.url or "").casefold()
    if "/login" in url or visible_marker(page, ("扫码登录", "验证码登录", "密码登录", "登录后免费畅享高清视频")):
        return "session_expired"
    if visible_marker(page, ("安全验证", "完成验证", "滑动验证", "验证码")):
        return "captcha"
    if visible_marker(page, ("访问频繁", "操作频繁", "网络错误", "系统繁忙", "存在风险")):
        return "risk_control"
    return ""


def parse_comment_item(item: Any) -> str:
    body = item.locator('.FduGc_lz > .Sh1Da424')
    if body.count() == 1:
        # The sibling .comment-item-tag is UI metadata, not author text.
        # Emoji images carry the actual visible token in their alt attribute.
        return body.first.evaluate('''el => {
            const copy = el.cloneNode(true);
            for (const img of copy.querySelectorAll('img')) {
                img.replaceWith(document.createTextNode(img.getAttribute('alt') || ''));
            }
            for (const br of copy.querySelectorAll('br')) br.replaceWith(document.createTextNode('\\n'));
            return copy.textContent.trim();
        }''')
    lines = [line.strip() for line in item.inner_text(timeout=3000).splitlines() if line.strip()]
    if not lines:
        return ""
    start = 2 if len(lines) > 1 and lines[1] == "..." else 1
    end = len(lines)
    for index in range(start, len(lines)):
        if TIME_LINE.match(lines[index]):
            end = index
            break
    content = " ".join(lines[start:end]).strip()
    return content


def detect_comment_image(item: Any) -> bool | None:
    """Detect a comment attachment while excluding ordinary avatars and emoji."""
    explicit_selectors = (
        '[data-e2e*="comment-image"]',
        '[data-e2e*="comment-picture"]',
        '[class*="comment-image"] img',
        '[class*="comment-picture"] img',
        '[class*="image-list"] img',
    )
    try:
        if item.locator(", ".join(explicit_selectors)).count() > 0:
            return True
        return bool(item.locator("img").evaluate_all(
            """images => images.some(img => {
                const rect = img.getBoundingClientRect();
                const label = `${img.alt || ''} ${img.className || ''} ${img.parentElement?.className || ''} ${img.parentElement?.parentElement?.className || ''}`.toLowerCase();
                if (/avatar|head|user|emoji|icon|头像|表情/.test(label)) return false;
                return rect.width >= 56 && rect.height >= 56 && img.naturalWidth >= 80 && img.naturalHeight >= 80;
            })"""
        ))
    except Exception:
        return None


def parse_declared_comment_count(text: str) -> int | None:
    text = (text or "").strip()
    if text.isdigit():
        return int(text)
    if "抢首评" in text:
        return 0
    return None


def read_declared_comment_count(page: Any, timeout_ms: int = 1500) -> int | None:
    try:
        text = page.locator('[data-e2e="feed-comment-icon"]').first.inner_text(timeout=timeout_ms).strip()
    except Exception:
        return None
    return parse_declared_comment_count(text)


def should_stop_loading(*, declared: int | None, captured: int, terminal: bool, stable: int) -> bool:
    """Stop scrolling only at a terminal/count boundary or after a stable fallback."""
    return terminal or (declared is not None and captured >= declared) or stable >= 4


def capture_target(page: Any, target: Dict[str, Any], max_scrolls: int) -> Dict[str, Any]:
    page.goto(target["url"], wait_until="domcontentloaded", timeout=30_000)
    status = security_status(page)
    if status:
        return {"capture_status": status, "capture_complete": False, "comments": []}

    declared = read_declared_comment_count(page)
    if declared == 0:
        status = security_status(page)
        if status:
            return {"capture_status": status, "capture_complete": False, "comments": []}
        return {
            "capture_status": "ok",
            "capture_complete": True,
            "declared_comment_count": 0,
            "captured_comment_count": 0,
            "image_detection_complete": True,
            "image_detection_method": "attachment-selectors-and-dimensions",
            "comments": [],
            "resolved_url": page.url,
        }

    comment_list = page.locator('[data-e2e="comment-list"]')
    try:
        comment_list.wait_for(state="visible", timeout=12_000)
    except PlaywrightTimeoutError:
        status = security_status(page)
        return {
            "capture_status": status or "inaccessible",
            "capture_complete": False,
            "comments": [],
            "warnings": ["comment_list_not_visible"],
        }
    if declared is None:
        declared = read_declared_comment_count(page, timeout_ms=800)

    previous = -1
    stable = 0
    for _ in range(max_scrolls):
        terminal = visible_marker(page, ("暂时没有更多评论", "没有更多评论"))
        items = page.locator('[data-e2e="comment-item"]')
        count = items.count()
        stable = stable + 1 if count == previous else 0
        previous = count
        if should_stop_loading(declared=declared, captured=count, terminal=terminal, stable=stable):
            break
        if count:
            try:
                items.last.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
        page.mouse.wheel(0, 700)
        page.wait_for_timeout(random.randint(900, 1500))
        status = security_status(page)
        if status:
            return {"capture_status": status, "capture_complete": False, "comments": []}

    terminal = visible_marker(page, ("暂时没有更多评论", "没有更多评论"))
    items = page.locator('[data-e2e="comment-item"]')
    comments: List[Dict[str, Any]] = []
    image_detection_complete = True
    for index in range(items.count()):
        try:
            item = items.nth(index)
            text = parse_comment_item(item)
            image_state = detect_comment_image(item)
        except Exception:
            image_detection_complete = False
            continue
        if image_state is None:
            image_detection_complete = False
        if not text and image_state is not True:
            continue
        comment: Dict[str, Any] = {"dom_index": index, "text": text}
        if image_state is not None:
            comment["has_image"] = image_state
            comment["comment_type"] = "text+image" if text and image_state else "image" if image_state else "text"
        comments.append(comment)
    complete = terminal or (declared is not None and len(comments) >= declared)
    result: Dict[str, Any] = {
        "capture_status": "ok",
        "capture_complete": complete,
        "declared_comment_count": declared,
        "captured_comment_count": len(comments),
        "image_detection_complete": image_detection_complete,
        "image_detection_method": "attachment-selectors-and-dimensions",
        "comments": comments,
        "resolved_url": page.url,
    }
    if not image_detection_complete:
        result.setdefault("warnings", []).append("comment_image_presence_incomplete")
    if not complete:
        result.setdefault("warnings", []).append("comments_pagination_incomplete")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", action="append", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--profile", default=str(default_profile("douyin")))
    parser.add_argument("--delay-min", type=float, default=5.0)
    parser.add_argument("--delay-max", type=float, default=8.0)
    parser.add_argument("--pace-mode", choices=("adaptive", "fixed"), default="adaptive")
    parser.add_argument("--minimum-rest", type=float, default=1.5)
    parser.add_argument("--break-every", type=int, default=10)
    parser.add_argument("--break-min", type=float, default=20.0)
    parser.add_argument("--break-max", type=float, default=35.0)
    parser.add_argument("--max-scrolls", type=int, default=20)
    parser.add_argument("--only-target-key")
    parser.add_argument("--skip-existing-ok", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="Recheck selected targets instead of resuming this job")
    args = parser.parse_args()
    if (
        args.delay_min < 0 or args.delay_max < args.delay_min or args.minimum_rest < 0
        or args.break_every < 0 or args.break_min < 0 or args.break_max < args.break_min
    ):
        parser.error("invalid delay bounds")

    targets = targets_from_expected(args.expected)
    if args.only_target_key:
        targets = [item for item in targets if item["target_key"] == args.only_target_key]
        if not targets:
            parser.error("--only-target-key not found")

    output = Path(args.output)
    existing: Dict[str, Dict[str, Any]] = {}
    if output.exists():
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
            existing = {str(item.get("target_key")): item for item in payload.get("observations", [])}
        except (OSError, ValueError, json.JSONDecodeError):
            existing = {}

    total_targets = len(targets)
    targets = pending_targets(targets, existing, "douyin", "target_key", refresh=args.refresh)
    print(f"[resume-plan] total={total_targets} reused={total_targets-len(targets)} pending={len(targets)}", flush=True)
    if not targets:
        return 0
    stop_reason = ""
    processed_count = 0
    initial_network = network_fingerprint()
    with ProfileLock(args.profile) as profile_lock:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_lock.profile),
                executable_path=find_chrome(CHROME),
                headless=False,
                viewport={"width": 1440, "height": 900},
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                for index, target in enumerate(targets, start=1):
                    key = target["target_key"]
                    if network_fingerprint() != initial_network:
                        existing[key] = {
                            "platform": "douyin", "target_key": key,
                            "capture_status": "network_changed", "capture_complete": False,
                            "captured_at": datetime.now(timezone.utc).isoformat(),
                            "warnings": ["network_route_or_proxy_changed_during_batch"],
                            "comments": [], "source_refs": target["source_refs"],
                        }
                        stop_reason = "network_changed"
                        atomic_write(output, {"observations": list(existing.values()), "stopped": True, "stop_reason": stop_reason})
                        print(f"[{index}/{len(targets)}] stop {key} status=network_changed", flush=True)
                        break
                    captured_at = datetime.now(timezone.utc).isoformat()
                    target_started = time.monotonic()
                    try:
                        result = capture_target(page, target, args.max_scrolls)
                    except Exception as exc:
                        result = {
                            "capture_status": "inaccessible",
                            "capture_complete": False,
                            "comments": [],
                            "warnings": [f"capture_error:{type(exc).__name__}"],
                        }
                    result["capture_duration_ms"] = round((time.monotonic() - target_started) * 1000)
                    observation = {
                        "platform": "douyin",
                        "parser_revision": PARSER_REVISIONS["douyin"],
                        "target_key": key,
                        "captured_at": captured_at,
                        "source_refs": target["source_refs"],
                        **result,
                    }
                    existing[key] = observation
                    processed_count += 1
                    status = observation["capture_status"]
                    print(
                        f"[{index}/{len(targets)}] {status} {key} comments={len(observation.get('comments', []))} "
                        f"complete={observation.get('capture_complete')}",
                        flush=True,
                    )
                    if status in {"session_expired", "captcha", "risk_control"}:
                        stop_reason = status
                    atomic_write(output, {"observations": list(existing.values()), "stopped": bool(stop_reason), "stop_reason": stop_reason})
                    if stop_reason:
                        break
                    if not has_remaining_target(index, len(targets)):
                        continue
                    if args.break_every and processed_count % args.break_every == 0:
                        pause = random.uniform(args.break_min, args.break_max)
                        print(f"[paced-break] seconds={pause:.1f}", flush=True)
                        time.sleep(pause)
                        continue
                    elapsed = time.monotonic() - target_started
                    pause = pause_seconds(
                        elapsed,
                        args.delay_min,
                        args.delay_max,
                        adaptive=args.pace_mode == "adaptive",
                        healthy=status == "ok" and observation.get("capture_complete") is True,
                        minimum_rest=args.minimum_rest,
                    )
                    print(f"[target-pause] seconds={pause:.1f} capture={elapsed:.1f}s mode={args.pace_mode}", flush=True)
                    time.sleep(pause)
            finally:
                context.close()
    return 3 if stop_reason else 0


if __name__ == "__main__":
    configure_stdio()
    raise SystemExit(main())
