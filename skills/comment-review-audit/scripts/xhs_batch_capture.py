#!/usr/bin/env python3
"""Capture unique Xiaohongshu targets from XLSX files in one read-only browser session."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET

from xhs_capture import flatten_comments, has_more, normalize_comment
from capture_pacing import has_remaining_target, pause_seconds
from capture_resume import pending_targets, PARSER_REVISIONS
from xhs_persistent import persistent_client
from session_environment import network_fingerprint
from xhs_visible_login import verification_status, wait_for_owner_confirmation


NOTE_PATTERNS = (
    re.compile(r"/(?:explore|discovery/item)/([A-Za-z0-9]+)"),
    re.compile(r"[?&](?:note_id|noteId)=([A-Za-z0-9]+)"),
)
XHS_TIME_LINE = re.compile(
    r"^(?:刚刚|今天|昨天|前天|\d+\s*(?:秒|分钟|小时|天|周|月|年)前|\d{1,2}-\d{1,2}|\d{4}-\d{1,2}-\d{1,2})"
)


def load_xhs_modules():
    try:
        from xhs_cli.auth import cookie_str_to_dict, get_cookie_string
        from xhs_cli.client import XhsClient
        from xhs_cli.exceptions import DataFetchError, LoginError
        return cookie_str_to_dict, get_cookie_string, XhsClient, DataFetchError, LoginError
    except ImportError:
        candidates = sorted((Path.home() / ".local/share/uv/tools/xhs-cli/lib").glob("python*/site-packages"))
        if not candidates:
            raise RuntimeError("xhs_cli_not_installed")
        # Keep the tool environment available for xhs_cli without shadowing
        # this Python's compiled dependencies such as Playwright/greenlet.
        sys.path.append(str(candidates[-1]))
        from xhs_cli.auth import cookie_str_to_dict, get_cookie_string
        from xhs_cli.client import XhsClient
        from xhs_cli.exceptions import DataFetchError, LoginError
        return cookie_str_to_dict, get_cookie_string, XhsClient, DataFetchError, LoginError


def note_id_from_url(url: str) -> str:
    for pattern in NOTE_PATTERNS:
        match = pattern.search(url or "")
        if match:
            return match.group(1)
    return ""


def target_from_url(value: str) -> Optional[Dict[str, Any]]:
    match = re.search(r'https?://[^\s<>"\u3000]+', value or "")
    if not match:
        return None
    url = match.group(0)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in ("xhslink.cn", "xhslink.com", "www.xhslink.com", "www.xhslink.cn"):
        return {"note_id": parsed.path.rstrip("/").rsplit("/", 1)[-1],
                "token": "", "short_url": url, "sources": []}
    if host != "xiaohongshu.com" and not host.endswith(".xiaohongshu.com"):
        return None
    note_id = note_id_from_url(url)
    if not note_id:
        return None
    return {"note_id": note_id,
            "token": parse_qs(parsed.query).get("xsec_token", [""])[0], "sources": []}


def workbook_column_a(path: str) -> Iterable[tuple[str, int, str]]:
    """Yield sheet name, row number, and decoded A-cell text using only stdlib."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rel_ns = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
    doc_rel = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    with zipfile.ZipFile(path) as archive:
        shared: List[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall("m:si", ns):
                shared.append("".join(node.text or "" for node in item.findall(".//m:t", ns)))
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {item.attrib["Id"]: item.attrib["Target"] for item in relationships.findall("r:Relationship", rel_ns)}
        for sheet in workbook.findall("m:sheets/m:sheet", ns):
            sheet_name = sheet.attrib.get("name", "")
            target = targets.get(sheet.attrib.get(doc_rel, ""), "")
            if target.startswith("/"):
                member = target.lstrip("/")
            elif target.startswith("xl/"):
                member = target
            else:
                member = "xl/" + target.lstrip("/")
            root = ET.fromstring(archive.read(member))
            for row in root.findall("m:sheetData/m:row", ns):
                row_number = int(row.attrib.get("r", "0") or 0)
                if row_number < 2:
                    continue
                cell = next((item for item in row.findall("m:c", ns) if item.attrib.get("r", "").startswith("A")), None)
                if cell is None:
                    yield sheet_name, row_number, ""
                    continue
                cell_type = cell.attrib.get("t", "")
                if cell_type == "inlineStr":
                    value = "".join(node.text or "" for node in cell.findall(".//m:t", ns))
                else:
                    value_node = cell.find("m:v", ns)
                    raw = value_node.text if value_node is not None and value_node.text else ""
                    if cell_type == "s" and raw.isdigit() and int(raw) < len(shared):
                        value = shared[int(raw)]
                    else:
                        value = raw
                yield sheet_name, row_number, value


def targets_from_workbooks(paths: Iterable[str]) -> List[Dict[str, Any]]:
    targets: Dict[str, Dict[str, Any]] = {}
    for input_path in paths:
        current_url_by_sheet: Dict[str, str] = {}
        for sheet_name, row_number, value in workbook_column_a(input_path):
            current_url = current_url_by_sheet.get(sheet_name, "")
            if isinstance(value, str) and re.search(r"https?://", value):
                current_url = value.strip()
                current_url_by_sheet[sheet_name] = current_url
            if not current_url:
                continue
            candidate = target_from_url(current_url)
            if not candidate:
                continue
            note_id = candidate["note_id"]
            token = candidate["token"]
            target = targets.setdefault(note_id, candidate)
            if token and not target["token"]:
                target["token"] = token
            source = {"file": Path(input_path).name, "sheet": sheet_name, "row": row_number}
            if source not in target["sources"]:
                target["sources"].append(source)
    return list(targets.values())


def declared_comment_count(detail: Dict[str, Any]) -> Optional[int]:
    note = detail.get("note", detail) if isinstance(detail, dict) else {}
    interact = note.get("interactInfo", note.get("interact_info", {})) if isinstance(note, dict) else {}
    value = interact.get("commentCount", interact.get("comment_count")) if isinstance(interact, dict) else None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def hold_verification(client: Any) -> bool:
    """Keep the actual challenge open until explicit owner confirmation."""
    from xhs_cli.auth import REQUIRED_COOKIES, save_cookies
    if not getattr(client, "_page", None):
        return False
    client._page.bring_to_front()
    try:
        wait_for_owner_confirmation(client, REQUIRED_COOKIES, save_cookies)
    except (EOFError, KeyboardInterrupt):
        print("VERIFICATION_INPUT_UNAVAILABLE — checkpoint retained", flush=True)
        return False
    return True


def login_gate_visible(client: Any) -> bool:
    page = getattr(client, "_page", None)
    if page is None:
        return True
    url = str(getattr(page, "url", "") or "").casefold()
    if "/login" in url or "website-login" in url:
        return True
    strong_markers = (
        "手机号登录",
        "输入验证码",
        "登录后推荐更懂你的笔记",
        "登录后查看更多评论",
    )
    try:
        for marker in strong_markers:
            locator = page.get_by_text(marker, exact=False)
            if locator.count() and locator.first.is_visible():
                return True
    except Exception:
        return False
    return False


def parse_dom_comment(item: Any) -> Dict[str, Any]:
    body = item.locator(".content .note-text")
    if body.count() == 1:
        return {
            "text": body.first.inner_text(timeout=3000).strip(),
            "has_image": item.locator(".comment-picture img").count() > 0,
            "text_source": "dom_note_text",
        }
    lines = [line.strip() for line in item.inner_text(timeout=3000).splitlines() if line.strip()]
    if len(lines) < 2:
        return {"text": "", "has_image": item.locator(".comment-picture img").count() > 0}
    end = len(lines)
    for index in range(1, len(lines)):
        if XHS_TIME_LINE.match(lines[index]):
            end = index
            break
    return {
        "text": " ".join(lines[1:end]).strip(),
        "has_image": item.locator(".comment-picture img").count() > 0,
    }


def dom_capture_complete(declared: Optional[int], captured: int, terminal_visible: bool) -> bool:
    """A visible terminal is authoritative when platform counters are stale."""
    return terminal_visible or (declared is not None and captured >= declared)


def visible_terminal(container: Any) -> bool:
    for marker in ("- THE END -", "没有更多评论", "暂时没有更多评论"):
        try:
            locator = container.get_by_text(marker, exact=False)
            if locator.count() and locator.first.is_visible():
                return True
        except Exception:
            continue
    return False


def apply_risk_averse_profile(args: argparse.Namespace) -> None:
    """Raise pacing and DOM-loading safeguards without changing accounts or routes."""
    args.delay_min = max(args.delay_min, 12.0)
    args.delay_max = max(args.delay_max, 18.0)
    args.minimum_rest = max(args.minimum_rest, 4.0)
    args.break_every = min(args.break_every, 8) if args.break_every else 8
    args.break_min = max(args.break_min, 60.0)
    args.break_max = max(args.break_max, 90.0)
    args.dom_max_rounds = max(args.dom_max_rounds, 45)
    args.incomplete_dom_retry_rounds = max(args.incomplete_dom_retry_rounds, 20)
    args.session_settle_min = max(args.session_settle_min, 8.0)
    args.session_settle_max = max(args.session_settle_max, 15.0)
    args.warmup_targets = max(args.warmup_targets, 3)
    args.warmup_min = max(args.warmup_min, 20.0)
    args.warmup_max = max(args.warmup_max, 35.0)


def expand_dom_comments(page: Any, max_rounds: int = 30) -> tuple[List[Dict[str, Any]], bool, bool]:
    """Expand reply groups and lazy-load comments without social interactions."""
    container = page.locator(".comments-container")
    if not container.count() or not container.first.is_visible():
        return [], False, True
    stable = 0
    previous_count = -1
    for _ in range(max_rounds):
        clicked = False
        expanders = container.get_by_text(
            re.compile(r"(?:展开\s*\d+\s*条回复|展开更多回复|更多回复)"),
            exact=False,
        )
        for index in range(expanders.count()):
            expander = expanders.nth(index)
            try:
                if expander.is_visible():
                    expander.scroll_into_view_if_needed(timeout=2000)
                    expander.click(timeout=3000)
                    page.wait_for_timeout(random.randint(700, 1100))
                    clicked = True
            except Exception:
                continue
        items = container.locator(".comment-item")
        count = items.count()
        stable = stable + 1 if count == previous_count and not clicked else 0
        previous_count = count
        if count:
            try:
                items.last.scroll_into_view_if_needed(timeout=3000)
                page.mouse.wheel(0, 600)
            except Exception:
                pass
        page.wait_for_timeout(random.randint(800, 1300))
        terminal = visible_terminal(container)
        if terminal and stable >= 1:
            break
        if stable >= 4:
            break
    comments = []
    extraction_complete = True
    items = container.locator(".comment-item")
    for index in range(items.count()):
        try:
            comment = parse_dom_comment(items.nth(index))
        except Exception:
            extraction_complete = False
            continue
        if comment["text"] or comment.get("has_image") is True:
            comments.append(comment)
    return comments, visible_terminal(container), extraction_complete


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True, help="Source XLSX; repeat for multiple files")
    parser.add_argument("--output", required=True)
    parser.add_argument("--headed", action="store_true", help="Use a visible browser window")
    parser.add_argument("--wait-for-confirmation", action="store_true",
                        help="Keep a challenge visible; resume only after owner confirmation via stdin (use a PTY)")
    parser.add_argument("--delay-min", type=float, default=6.0)
    parser.add_argument("--delay-max", type=float, default=10.0)
    parser.add_argument("--pace-mode", choices=("adaptive", "fixed"), default="adaptive")
    parser.add_argument("--minimum-rest", type=float, default=1.5)
    parser.add_argument("--break-every", type=int, default=12)
    parser.add_argument("--break-min", type=float, default=25.0)
    parser.add_argument("--break-max", type=float, default=45.0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--only-note-id", action="append", help="Capture one target key; repeat to select several targets")
    parser.add_argument("--skip-existing-ok", action="store_true", help="Compatibility flag; only fresh complete captures are reused")
    parser.add_argument("--refresh", action="store_true", help="Recheck selected targets instead of resuming this job")
    parser.add_argument("--expand-dom", action="store_true", help="Expand lazy-loaded comments and reply groups in the visible page")
    parser.add_argument("--dom-max-rounds", type=int, default=30)
    parser.add_argument("--incomplete-dom-retry-rounds", type=int, default=0,
                        help="Extra same-page DOM expansion only when the first pass is incomplete")
    parser.add_argument("--session-settle-min", type=float, default=0.0,
                        help="Quiet wait after opening the authorized session")
    parser.add_argument("--session-settle-max", type=float, default=0.0)
    parser.add_argument("--warmup-targets", type=int, default=0,
                        help="Initial targets that use a longer inter-target pause")
    parser.add_argument("--warmup-min", type=float, default=0.0)
    parser.add_argument("--warmup-max", type=float, default=0.0)
    parser.add_argument("--risk-averse", action="store_true",
                        help="Use a slower single-account profile with extra same-page loading safeguards")
    parser.add_argument(
        "--profile",
        default=str(Path.home() / ".comment-review-audit/xhs-profile"),
        help="Persistent ordinary-Chrome profile",
    )
    args = parser.parse_args(argv)
    if args.wait_for_confirmation and not args.headed:
        parser.error("--wait-for-confirmation requires --headed")
    if args.risk_averse:
        apply_risk_averse_profile(args)
    if (
        args.delay_min < 0 or args.delay_max < args.delay_min or args.minimum_rest < 0
        or args.break_every < 0 or args.break_min < 0 or args.break_max < args.break_min
        or args.incomplete_dom_retry_rounds < 0 or args.session_settle_min < 0
        or args.session_settle_max < args.session_settle_min or args.warmup_targets < 0
        or args.warmup_min < 0 or args.warmup_max < args.warmup_min
    ):
        parser.error("invalid delay bounds")

    targets = targets_from_workbooks(args.input)
    if args.only_note_id:
        targets = [target for target in targets if target["note_id"] in set(args.only_note_id)]
        if not targets:
            parser.error("--only-note-id was not found in the input workbooks")
    if args.limit is not None:
        targets = targets[: max(0, args.limit)]
    output_path = Path(args.output)
    existing: Dict[str, Dict[str, Any]] = {}
    if output_path.exists():
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            existing = {str(item.get("target_key")): item for item in payload.get("observations", [])}
        except (OSError, ValueError, json.JSONDecodeError):
            existing = {}

    total_targets = len(targets)
    targets = pending_targets(targets, existing, "xiaohongshu", "note_id", refresh=args.refresh)
    print(f"[resume-plan] total={total_targets} reused={total_targets-len(targets)} pending={len(targets)}", flush=True)
    if not targets:
        return 0
    cookie_str_to_dict, get_cookie_string, XhsClient, DataFetchError, LoginError = load_xhs_modules()
    cookie = get_cookie_string()
    client = persistent_client(XhsClient, cookie_str_to_dict(cookie or ""), args.profile, args.headed)
    stop_reason = ""
    processed_count = 0
    consecutive_errors = 0
    initial_network = network_fingerprint()
    try:
        try:
            client.start()
            if login_gate_visible(client):
                raise LoginError("login required: visible login gate")
        except LoginError as exc:
            stop_reason = verification_status(client, exc)
            atomic_write(output_path, {"observations": list(existing.values()), "stopped": True,
                                       "stop_reason": stop_reason, "stop_phase": "startup"})
            print(f"[startup] stop status={stop_reason}", flush=True)
            confirmed = args.wait_for_confirmation and hold_verification(client)
            return 4 if confirmed else 3
        if args.session_settle_max:
            settle = random.uniform(args.session_settle_min, args.session_settle_max)
            print(f"[session-settle] seconds={settle:.1f} profile={'risk-averse' if args.risk_averse else 'standard'}", flush=True)
            time.sleep(settle)
        for index, target in enumerate(targets, start=1):
            note_id = target["note_id"]
            if network_fingerprint() != initial_network:
                existing[note_id] = {
                    "platform": "xiaohongshu", "target_key": note_id,
                    "capture_status": "network_changed", "capture_complete": False,
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "warnings": ["network_route_or_proxy_changed_during_batch"],
                    "comments": [], "source_refs": target["sources"],
                }
                stop_reason = "network_changed"
                atomic_write(output_path, {"observations": list(existing.values()), "stopped": True, "stop_reason": stop_reason})
                print(f"[{index}/{len(targets)}] stop {note_id} status=network_changed", flush=True)
                break
            captured_at = datetime.now(timezone.utc).isoformat()
            target_started = time.monotonic()
            try:
                resolved_note_id, token = note_id, target["token"]
                if target.get("short_url"):
                    client._page.goto(target["short_url"], wait_until="domcontentloaded", timeout=30000)
                    client._page.wait_for_timeout(2000)
                    if login_gate_visible(client):
                        raise LoginError("login required: visible login gate")
                    visible = client._page.locator("body").inner_text(timeout=3000)
                    if any(marker in visible for marker in ("安全验证", "请完成验证", "验证码", "安全限制", "IP存在风险", "访问频繁")):
                        raise LoginError("captcha or risk control on short link")
                    resolved = target_from_url(client._page.url)
                    if not resolved or resolved.get("short_url"):
                        raise DataFetchError("short_link_unresolved")
                    resolved_note_id, token = resolved["note_id"], resolved["token"]
                detail = client.get_note_detail(resolved_note_id, token)
                raw_comments = client.get_note_comments(resolved_note_id, token, max_comments=0)
                if login_gate_visible(client):
                    raise LoginError("login required: visible login gate")
                flat = flatten_comments(raw_comments)
                declared = declared_comment_count(detail)
                normalized_comments = [normalize_comment(item) for item in flat]
                dom_terminal = False
                dom_parse_failed = False
                if args.expand_dom and (has_more(raw_comments) or (declared is not None and len(flat) < declared)):
                    dom_comments, dom_terminal, parsed_complete = expand_dom_comments(client._page, args.dom_max_rounds)
                    dom_parse_failed = not parsed_complete
                    if len(dom_comments) >= len(normalized_comments):
                        normalized_comments = dom_comments
                    incomplete_after_first_pass = dom_parse_failed or not dom_capture_complete(declared, len(normalized_comments), dom_terminal)
                    if incomplete_after_first_pass and args.incomplete_dom_retry_rounds:
                        retry_comments, retry_terminal, retry_complete = expand_dom_comments(client._page, args.incomplete_dom_retry_rounds)
                        dom_parse_failed = not retry_complete
                        if len(retry_comments) >= len(normalized_comments):
                            normalized_comments = retry_comments
                        dom_terminal = dom_terminal or retry_terminal
                if args.expand_dom:
                    incomplete = dom_parse_failed or not dom_capture_complete(declared, len(normalized_comments), dom_terminal)
                else:
                    incomplete = has_more(raw_comments) or (
                        declared is not None and len(normalized_comments) < declared
                    )
                observation = {
                    "platform": "xiaohongshu",
                    "parser_revision": PARSER_REVISIONS["xiaohongshu"],
                    "target_key": note_id,
                    "resolved_target_key": resolved_note_id,
                    "capture_status": "ok",
                    "capture_complete": not incomplete,
                    "image_detection_complete": not dom_parse_failed,
                    "captured_at": captured_at,
                    "declared_comment_count": declared,
                    "captured_comment_count": len(normalized_comments),
                    "pacing_profile": "risk-averse" if args.risk_averse else "standard",
                    "comments": normalized_comments,
                    "source_refs": target["sources"],
                }
                if incomplete:
                    observation["warnings"] = ["comment_dom_extraction_incomplete" if dom_parse_failed else "comments_pagination_incomplete"]
                elif dom_terminal and declared is not None and len(normalized_comments) < declared:
                    observation["warnings"] = ["visible_terminal_with_stale_declared_count"]
                existing[note_id] = observation
                processed_count += 1
                consecutive_errors = 0
                print(
                    f"[{index}/{len(targets)}] ok {note_id} comments={len(normalized_comments)} complete={not incomplete}",
                    flush=True,
                )
            except LoginError as exc:
                status = verification_status(client, exc)
                existing[note_id] = {
                    "platform": "xiaohongshu", "target_key": note_id,
                    "capture_status": status, "capture_complete": False,
                    "captured_at": captured_at, "warnings": ["platform_security_stop"],
                    "comments": [], "source_refs": target["sources"],
                }
                stop_reason = status
                print(f"[{index}/{len(targets)}] stop {note_id} status={status}", flush=True)
            except DataFetchError:
                existing[note_id] = {
                    "platform": "xiaohongshu", "target_key": note_id,
                    "capture_status": "inaccessible", "capture_complete": False,
                    "captured_at": captured_at, "warnings": ["note_data_unavailable"],
                    "comments": [], "source_refs": target["sources"],
                }
                print(f"[{index}/{len(targets)}] inaccessible {note_id}", flush=True)
                processed_count += 1
                consecutive_errors += 1
                if consecutive_errors >= 2:
                    stop_reason = "repeated_navigation_failure"
            existing[note_id]["capture_duration_ms"] = round((time.monotonic() - target_started) * 1000)
            atomic_write(output_path, {"observations": list(existing.values()), "stopped": bool(stop_reason), "stop_reason": stop_reason})
            if stop_reason:
                if args.wait_for_confirmation and stop_reason in ("captcha", "session_expired", "risk_control"):
                    if hold_verification(client):
                        return 4
                break
            if not has_remaining_target(index, len(targets)):
                continue
            if args.break_every and processed_count and processed_count % args.break_every == 0:
                pause = random.uniform(args.break_min, args.break_max)
                print(f"[paced-break] seconds={pause:.1f}", flush=True)
                time.sleep(pause)
                continue
            elapsed = time.monotonic() - target_started
            observation = existing[note_id]
            pause = pause_seconds(
                elapsed,
                args.delay_min,
                args.delay_max,
                adaptive=args.pace_mode == "adaptive",
                healthy=observation.get("capture_status") == "ok" and observation.get("capture_complete") is True,
                minimum_rest=args.minimum_rest,
            )
            if processed_count <= args.warmup_targets:
                pause = max(pause, random.uniform(args.warmup_min, args.warmup_max))
                mode = f"{args.pace_mode}+warmup"
            else:
                mode = args.pace_mode
            print(f"[target-pause] seconds={pause:.1f} capture={elapsed:.1f}s mode={mode}", flush=True)
            time.sleep(pause)
    finally:
        client.close()
    return 3 if stop_reason else 0


if __name__ == "__main__":
    raise SystemExit(main())
