#!/usr/bin/env python3
"""Capture Douyin comments from expected-row JSON using a persistent visible Chrome profile."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import time
from collections import Counter
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
except ImportError:
    PlaywrightTimeoutError = TimeoutError  # parser-only hosts never call a live browser
from capture_pacing import has_remaining_target, pause_seconds
from capture_watchdog import paced_sleep, visible_progress
from capture_resume import pending_targets, recheck_scope, PARSER_REVISIONS
from audit_pass import rows_to_review
from profile_lock import ProfileLock, default_profile
from runtime_environment import configure_stdio
from session_environment import network_fingerprint
from browser_environment import find_chrome
from table_to_expected import douyin_video_id


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


def saved_observations(original, previous, current):
    changed = {key: item for key, item in current.items() if previous.get(key) is not item}
    return [item for item in original if item.get("platform") != "douyin" or str(item["target_key"]) not in changed] + list(changed.values())


def expected_rows(path: str) -> Iterable[Dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("rows", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError(f"expected rows are not a list: {path}")
    return rows


def capture_plan(paths: Iterable[str]):
    targets: Dict[str, Dict[str, Any]] = {}
    all_rows = []
    for input_path in paths:
        rows = list(expected_rows(input_path))
        all_rows.extend(rows)
        for row in rows_to_review(rows):
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
    return list(targets.values()), recheck_scope(all_rows)


ROW = '[data-e2e="comment-item"], [data-e2e="comment-reply-item"], [data-e2e="reply-item"], [data-e2e="comment-item-reply"]'
BODY = '.FduGc_lz > .Sh1Da424, [data-e2e="comment-content"], [data-e2e="comment-text"], [data-e2e="comment-item-content"]'
FOLD = re.compile(r"^\s*(?:(?:展开|查看)(?:其余|剩余)?\s*(\d+)\s*条?\s*(?:回复|评论)|(?:展开更多|查看更多|更多)\s*(?:回复|评论)|全部回复)\s*[⌄∨v]?\s*$")
END = re.compile(r"^\s*(?:暂时没有更多评论|没有更多评论|没有更多了)\s*$")
EMPTY = re.compile(r"^\s*(?:暂无评论|暂时没有评论|还没有评论|快来抢首评吧|抢首评)\s*[哦～~！!。]?\s*$")
LOADING = '[aria-busy="true"], [role="progressbar"], [data-e2e="comment-loading"], .comments-loading, .reply-loading, .loading-more'


def outside(selector):
    return f':visible:not(:is({selector})):not(:is({selector}) *)'


def visible_marker(page: Any, markers: Iterable[str]) -> bool:
    outside_rows = page.locator(outside(ROW))
    return any(page.get_by_text(marker, exact=False).and_(outside_rows).count() for marker in markers)


def security_status(page: Any) -> str:
    url = str(page.url or "").casefold()
    if "/login" in url or visible_marker(page, ("扫码登录", "验证码登录", "密码登录", "登录后免费畅享高清视频")):
        return "session_expired"
    if visible_marker(page, ("安全验证", "完成验证", "滑动验证", "验证码")):
        return "captcha"
    if visible_marker(page, ("访问频繁", "操作频繁", "网络错误", "系统繁忙", "存在风险")):
        return "risk_control"
    return ""


def video_id(url):
    try:
        return douyin_video_id(url)
    except ValueError:
        return None


def on_assigned_video(url, target):
    resolved = video_id(url)
    if not resolved:
        return False
    try:
        expected = douyin_video_id(target["url"])
    except ValueError:
        return False
    key = str(target.get("target_key", ""))
    if (expected and expected != resolved) or (key.isdigit() and key != resolved):
        return False
    return bool(expected or key.isdigit() or urlsplit(target["url"]).hostname == "v.douyin.com")


def parse_item_lines(rendered):
    lines = [line.strip() for line in rendered.splitlines() if line.strip()]
    start = 2 if len(lines) > 1 and lines[1] == "..." else 1
    for end in range(start, len(lines)):
        if TIME_LINE.match(lines[end]):
            return " ".join(lines[start:end])
    # Without a timestamp there is no reliable boundary between a body and its action bar.
    return ""


# Keep nested replies' text, attachments and identifiers on their own rows.
ROW_FIELDS = r"""(rows, cfg) => rows.map(row => {
    const shown = el => el.getClientRects().length && getComputedStyle(el).visibility === 'visible';
    const own = query => [...row.querySelectorAll(query)].filter(el => el.closest(cfg.row) === row && shown(el));
    const matched = own(cfg.body);
    const bodies = matched.filter(el => !matched.some(other => el !== other && el.contains(other)));
    let body = null, raw = '';
    if (bodies.length === 1) {
        const bodyText = node => {
            if (node.nodeType === Node.TEXT_NODE) return node.textContent;
            if (node.nodeType !== Node.ELEMENT_NODE || node.hidden || node.getAttribute('aria-hidden') === 'true') return '';
            const style = getComputedStyle(node);
            if (style.display === 'none' || style.visibility !== 'visible') return '';
            if (node.tagName === 'IMG') return node.alt || '';
            if (node.tagName === 'BR') return '\n';
            return [...node.childNodes].map(bodyText).join('');
        };
        body = bodyText(bodies[0]).trim();
    } else if (!bodies.length && !row.querySelector(cfg.row)) {
        raw = row.innerText;
    }
    const explicit = own('[data-e2e*="comment-image"],[data-e2e*="comment-picture"],[class*="comment-image"] img,[class*="comment-picture"] img,[class*="image-list"] img');
    const image = explicit.length > 0 || own('img').some(img => {
        const rect = img.getBoundingClientRect();
        const label = [img.alt,img.className,img.parentElement?.className,img.parentElement?.parentElement?.className].join(' ').toLowerCase();
        return !/avatar|head|user|emoji|icon|头像|表情/.test(label) &&
            rect.width >= 56 && rect.height >= 56 && img.naturalWidth >= 80 && img.naturalHeight >= 80;
    });
    return {body, raw, image, id: row.getAttribute('data-comment-id') || row.getAttribute('data-cid')};
})"""


def read_rows(panel):
    items = panel.locator(f':is({ROW}):visible')
    rows = []
    parsed = True
    for item in items.evaluate_all(ROW_FIELDS, {"row": ROW, "body": BODY}):
        body = item["body"] if item["body"] is not None else parse_item_lines(item["raw"])
        if not body and not item["image"]:
            parsed = False
            continue
        image = item["image"]
        record = {"text": body, "has_image": image, "comment_type": ("text+image" if body else "image") if image else "text"}
        if item["id"]:
            record["comment_id"] = item["id"]
        rows.append(record)
    return rows, parsed


def identity(row):
    if row.get("comment_id"):
        return ("id", row["comment_id"])
    return ("anonymous", row["text"], row["has_image"])


def folded_controls(panel):
    return panel.get_by_text(FOLD).and_(panel.locator(outside(BODY)))


# Bind each click to the local thread and the pre-click multiset. A vanished
# button or a newly mounted comment in another thread does not confirm loading.
EXPANSION = r"""(control, cfg) => {
    const root = control.closest(cfg.row);
    const thread = root || control.parentElement;
    const panel = control.closest('[data-e2e="comment-list"]');
    const adjacent = root?.nextElementSibling;
    const replyList = adjacent?.matches('[data-e2e="comment-reply-list"],[data-e2e="reply-list"],.reply-list') ? adjacent : null;
    const signatures = () => {
        const counts = new Map();
        if (thread === panel || !thread.isConnected) return counts;
        const descendants = [...thread.querySelectorAll(cfg.row), ...(replyList?.querySelectorAll(cfg.row) || [])];
        for (const row of descendants) {
            if (!row.getClientRects().length || getComputedStyle(row).visibility !== 'visible') continue;
            const body = [...row.querySelectorAll(cfg.body)].find(el => el.closest(cfg.row) === row);
            const sig = row.getAttribute('data-comment-id') || row.getAttribute('data-cid') || body?.innerText.trim();
            if (sig) counts.set(sig, (counts.get(sig)||0)+1);
        }
        return counts;
    };
    const before = signatures();
    const label = control.innerText.trim();
    const state = JSON.stringify([label, [...before]]);
    if (cfg.clicked.get(control) === state) return null;
    cfg.clicked.set(control, state);
    const required = Number(label.match(/\d+/)?.[0] || 1);
    return {added: () => [...signatures()].reduce((n,[k,v]) => n + Math.max(0,v-(before.get(k)||0)),0), required};
}"""


def expand_controls(page, panel, clicked, pending):
    controls = folded_controls(panel)
    for i in reversed(range(controls.count())):
        control = controls.nth(i)
        if not control.count():
            continue
        receipt = control.evaluate_handle(EXPANSION, {"row": ROW, "body": BODY, "clicked": clicked})
        if not receipt.evaluate("r => !!r"):
            receipt.dispose()
            continue
        pending.append(receipt)
        try:
            control.click(timeout=2000)
            page.wait_for_function("r => r.added() >= 1", arg=receipt, timeout=3500).dispose()
            visible_progress()
        except PlaywrightTimeoutError:
            # The receipt will be checked again before any negative verdict.
            continue

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


def capture_target(page: Any, target: Dict[str, Any], max_scrolls: int) -> Dict[str, Any]:
    source = urlsplit(target["url"])
    host = (source.hostname or "").casefold()
    if source.scheme not in ("http", "https") or (host != "douyin.com" and not host.endswith(".douyin.com")):
        return {"capture_status": "inaccessible", "capture_complete": False, "comments": [],
                "warnings": ["target_identity_unconfirmed"]}
    page.goto(target["url"], wait_until="domcontentloaded", timeout=30_000)
    status = security_status(page)
    if status or not on_assigned_video(page.url, target):
        return {"capture_status": status or "inaccessible", "capture_complete": False, "comments": [],
                "warnings": [status or "target_identity_unconfirmed"]}
    if visible_marker(page, ("作品已删除", "视频不存在", "该视频已删除", "作品暂时无法观看")):
        return {"capture_status": "inaccessible", "capture_complete": False, "comments": [],
                "warnings": ["target_unavailable"]}

    panel = page.locator('[data-e2e="comment-list"]:visible')
    try:
        if not panel.count():
            try:
                panel.first.wait_for(state="visible", timeout=1200)
            except PlaywrightTimeoutError:
                opener = page.locator('[data-e2e="feed-comment-icon"]:visible')
                if opener.count() == 1 and not panel.count():
                    opener.click(timeout=2000)
                panel.first.wait_for(state="visible", timeout=10_000)
        if panel.count() != 1:
            return {"capture_status":"inaccessible", "capture_complete":False, "comments":[],
                    "warnings":["ambiguous_comment_panel"]}
    except PlaywrightTimeoutError:
        return {"capture_status": security_status(page) or "inaccessible", "capture_complete": False,
                "comments": [], "warnings": ["comment_list_not_visible"]}
    declared = read_declared_comment_count(page, timeout_ms=800)
    pending = []
    clicked = page.evaluate_handle("() => new WeakMap()")
    all_rows = {}
    previous = Counter()
    stable = 0
    recycled = False
    gap = False
    complete = False
    try:
        for iteration in range(max_scrolls + 1):
            if iteration < max_scrolls:
                expand_controls(page, panel, clicked, pending)
            rows, parsed = read_rows(panel)
            counts = Counter()
            gap |= not parsed
            for row in rows:
                key = identity(row)
                counts[key] += 1
                # Anonymous duplicates are counted only if actually seen together.
                all_rows[key, counts[key]] = row
            if counts - previous:
                visible_progress()
            recycled |= bool(previous - counts)
            stable = stable + 1 if previous == counts else 0
            previous = counts
            outside_comments = panel.locator(outside(ROW))
            terminal = panel.get_by_text(END).and_(outside_comments).count()
            empty = not rows and panel.get_by_text(EMPTY).and_(outside_comments).count()
            busy = panel.get_attribute("aria-busy") == "true" or panel.locator(f':is({LOADING}):visible').count()
            if ((terminal and all_rows) or empty) and not busy and not folded_controls(panel).count():
                confirmed = all(r.evaluate("s => s.added() >= s.required") for r in pending)
                if (parsed and not gap and confirmed and (not recycled or declared is not None)
                        and (declared is None or len(all_rows) >= declared)):
                    complete = True
                    break
            if stable >= 4 or iteration == max_scrolls:
                break
            items = panel.locator(f':is({ROW}):visible')
            if items.count():
                items.last.scroll_into_view_if_needed(timeout=3000)
            panel.hover(timeout=2000)
            page.mouse.wheel(0, 600)
            page.wait_for_timeout(random.randint(900, 1500))
            status = security_status(page)
            if status or not on_assigned_video(page.url, target):
                break
    finally:
        for receipt in pending:
            receipt.dispose()
        clicked.dispose()
    status = security_status(page)
    if status or not on_assigned_video(page.url, target):
        return {"capture_status": status or "inaccessible", "capture_complete": False, "comments": [],
                "warnings": [status or "target_identity_unconfirmed"]}
    comments = [dict(row, dom_index=i) for i, row in enumerate(all_rows.values())]
    result = {"capture_status": "ok", "capture_complete": complete, "declared_comment_count": declared,
              "captured_comment_count": len(comments), "image_detection_complete": not gap,
              "image_detection_method": "attachment-selectors-and-dimensions", "comments": comments,
              "resolved_target_key": video_id(page.url), "resolved_url": page.url.split("?", 1)[0]}
    if complete:
        result["capture_complete_source"] = "visible_comment_list_end"
    else:
        result["warnings"] = ["comments_pagination_incomplete"]
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
        or args.break_every < 0 or args.break_min < 0 or args.break_max < args.break_min or args.max_scrolls < 0
    ):
        parser.error("invalid delay bounds")

    targets, scope = capture_plan(args.expected)
    if args.only_target_key:
        targets = [item for item in targets if item["target_key"] == args.only_target_key]
        if not targets and not scope:
            parser.error("--only-target-key not found")

    output = Path(args.output)
    if any(output.resolve() == Path(path).resolve() or (output.exists() and output.samefile(path))
           for path in args.expected):
        parser.error("--output must differ from --expected")
    metadata = {"recheck_scope": scope} if scope else {}
    records = []
    if output.exists():
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
            existing_scope = payload.get("recheck_scope") if isinstance(payload, dict) else None
            if existing_scope != scope:
                parser.error("different audit pass: use a new --output for this recheck")
            records = payload.get("observations", []) if isinstance(payload, dict) else payload
            if not isinstance(records, list) or any(not isinstance(r, dict) or "target_key" not in r for r in records):
                parser.error("invalid observations in capture checkpoint")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.error(f"cannot read existing capture checkpoint: {exc}")

    previous = {str(item["target_key"]): item for item in records if item.get("platform") == "douyin"}
    existing = previous.copy()
    total_targets = len(targets)
    targets = pending_targets(targets, existing, "douyin", "target_key", refresh=args.refresh)
    print(f"[resume-plan] total={total_targets} reused={total_targets-len(targets)} pending={len(targets)}", flush=True)
    if not targets:
        if scope and not output.exists():
            atomic_write(output, {**metadata, "observations": [], "stopped": False})
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
                        atomic_write(output, {**metadata, "observations": saved_observations(records, previous, existing),
                                              "stopped": True, "stop_reason": stop_reason})
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
                    atomic_write(output, {**metadata, "observations": saved_observations(records, previous, existing),
                                          "stopped": bool(stop_reason), "stop_reason": stop_reason})
                    if stop_reason:
                        break
                    if not has_remaining_target(index, len(targets)):
                        continue
                    if args.break_every and processed_count % args.break_every == 0:
                        pause = random.uniform(args.break_min, args.break_max)
                        print(f"[paced-break] seconds={pause:.1f}", flush=True)
                        paced_sleep(pause)
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
                    paced_sleep(pause)
            finally:
                context.close()
    return 3 if stop_reason else 0


if __name__ == "__main__":
    configure_stdio()
    raise SystemExit(main())
