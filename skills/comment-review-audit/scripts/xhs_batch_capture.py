#!/usr/bin/env python3
"""Capture unique Xiaohongshu targets from XLSX files in one read-only browser session."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET

from xhs_capture import add_reply_relation, classify_failure, clean_comment_id, deleted_placeholder, has_more, normalized_comments
from capture_pacing import has_remaining_target, pause_seconds
from capture_resume import pending_targets, PARSER_REVISIONS
from review_comments import compare, platform_key, target_key, unwrap_rows
from xhs_persistent import persistent_client
from profile_lock import default_profile
from runtime_environment import configure_stdio
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
    return {"note_id": note_id, "source_url": url,
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
                address = f"A{row_number}"
                cell = next((item for item in row.iterfind("m:c", ns) if item.get("r") == address), None)
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
    seen_sources: set[tuple[str, str, str, int]] = set()
    for input_path in paths:
        source_file = Path(input_path).name
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
                target["source_url"] = candidate["source_url"]
            source_key = (note_id, source_file, sheet_name, row_number)
            if source_key not in seen_sources:
                seen_sources.add(source_key)
                target["sources"].append({"file": source_file, "sheet": sheet_name, "row": row_number})
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


DOM_COMMENT_FIELDS = r"""item => {
    const own = (element, selector) => [...element.querySelectorAll(selector)].filter(
        node => node.closest('.comment-item') === element);
    const commentId = element => element.getAttribute('data-comment-id') ||
        /^comment-([0-9a-f]{24})$/i.exec(element.id || '')?.[1] || null;
    const bodies = own(item, '.content .note-text');
    const ownText = bodies.length === 1;
    const nestedComments = item.querySelector('.comment-item');
    // A wrapper's aggregate innerText would also contain a nested comment.
    if (!ownText && nestedComments) return null;
    const directId = item.getAttribute('data-parent-comment-id');
    const targetId = item.getAttribute('data-target-comment-id');
    const group = item.closest('.parent-comment');
    const roots = group?.querySelectorAll(':scope > .comment-item');
    const isReplyInGroup = group && roots.length === 1 && item !== roots[0] &&
        item.closest('.reply-container')?.parentElement === group;
    const rootBodies = isReplyInGroup ? own(roots[0], '.content .note-text') : [];
    let parentId = directId;
    let parentText = null;
    if (!directId && !targetId) {
        // A directly nested comment (or an explicit direct-child group) supplies
        // an edge. A generic reply list may flatten a whole conversation.
        let parent = item.parentElement;
        let explicitGroup = false;
        if (parent?.matches('[data-direct-replies], [role="group"]')) {
            if (!parent.hasAttribute('data-direct-replies') && !item.matches('[role="treeitem"]')) parent = null;
            explicitGroup = !!parent;
            parent = parent?.parentElement;
        }
        if (parent?.matches('.comment-item') && (explicitGroup || parent.querySelectorAll('.comment-item').length === 1)) {
            parentId = commentId(parent);
            const texts = own(parent, '.content .note-text');
            if (texts.length === 1) parentText = texts[0].innerText.trim();
        }
    }
    return {
        text: ownText ? bodies[0].innerText : item.innerText,
        own_text: ownText,
        has_image: own(item, '.comment-picture img').length > 0,
        comment_id: commentId(item),
        parent_comment_id: parentId,
        target_comment_id: targetId,
        parent_text: parentText,
        thread_root_text: rootBodies.length === 1 ? rootBodies[0].innerText.trim() : null
    };
}"""


def dom_comment_record(fields: Dict[str, Any]) -> Dict[str, Any]:
    result = {"text": fields["text"].strip(), "has_image": fields["has_image"]}
    identifier = clean_comment_id(fields.get("comment_id"))
    if identifier:
        result["comment_id"] = identifier
    add_reply_relation(result, fields, root_list=False)
    if fields.get("thread_root_text") and deleted_placeholder(fields["thread_root_text"]):
        result["thread_root_deleted"] = True
    if fields.get("parent_text") and not deleted_placeholder(fields["parent_text"]):
        result["parent_text"] = fields["parent_text"]
    if deleted_placeholder(result["text"]):
        return {**result, "text": "", "has_image": False, "deleted": True}
    if fields["own_text"]:
        result["text_source"] = "dom_note_text"
        return result
    lines = [line.strip() for line in fields["text"].splitlines() if line.strip()]
    if len(lines) < 2:
        result["text"] = ""
        return result
    end = len(lines)
    for index in range(1, len(lines)):
        if XHS_TIME_LINE.match(lines[index]):
            end = index
            break
    result["text"] = " ".join(lines[1:end]).strip()
    if deleted_placeholder(result["text"]):
        result.update(text="", has_image=False, deleted=True)
    return result


def parse_dom_comment(item: Any) -> Dict[str, Any]:
    return dom_comment_record(item.evaluate(DOM_COMMENT_FIELDS))


def prefer_comment_snapshot(current, incoming):
    """Keep a single snapshot, enriching direct parents only for unambiguous IDs."""
    if not incoming:
        return current
    if not current:
        return incoming
    def quality(records):
        links = sum(bool(c.get("parent_comment_id") or c.get("parent_text")) for c in records)
        return (len(records), links, sum("comment_id" in c for c in records),
                sum(c.get("thread_root_deleted") is True for c in records))
    primary, secondary = (incoming, current) if quality(incoming) > quality(current) else (current, incoming)
    counts = Counter(comment.get("comment_id") for comment in primary)
    known = {}
    for comment in secondary:
        identifier = comment.get("comment_id")
        if identifier:
            known[identifier] = None if identifier in known else comment
    selected = []
    for comment in primary:
        identifier = comment.get("comment_id")
        other = known.get(identifier)
        if (other and counts[identifier] == 1 and comment["text"] == other["text"]
                and comment["has_image"] == other["has_image"]):
            if other.get("thread_root_deleted") is True:
                comment = {**comment, "thread_root_deleted": True}
            fields = ("parent_comment_id", "parent_text")
            if any(k in comment and k in other and comment[k] != other[k] for k in fields):
                comment = {k: v for k, v in comment.items() if k not in fields}
            else:
                comment = {**{k: other[k] for k in fields if k in other}, **comment}
        selected.append(comment)
    return selected


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


def group_reply_requirements(rows):
    """Group complete expected-row blocks; their source order resolves reply directives."""
    groups = {}
    for row in rows:
        if platform_key(row) == "xiaohongshu":
            groups.setdefault(target_key(row), []).append(row)
    return {key: rows for key, rows in groups.items() if any(row.get("reply_to") for row in rows)}


def load_reply_requirements(paths):
    return group_reply_requirements(
        row for path in paths for row in unwrap_rows(json.loads(Path(path).read_text(encoding="utf-8")), "rows"))


def reply_signature(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def unconfirmed_replies(rows, observation):
    if not rows:
        return 0
    results = compare(rows, [observation], amount_mode="expected-only")["results"]
    reasons = {"reply_parent_unconfirmed", "reply_source_parent_ambiguous"}
    return sum(bool(reasons.intersection(row["reason_codes"])) for row in results)


def needs_reply_recheck(rows, observation):
    """Decide whether a fresh checkpoint still needs a same-page check for these source rows."""
    if not rows:
        return False
    if not observation or observation.get("capture_status") != "ok":
        return True
    check = observation.get("reply_verification") or {}
    if check.get("page_attempted") and check.get("requirements_sha256") == reply_signature(rows):
        if check.get("panel_available") or check.get("attempt_count", 1) >= 2:
            return False
    return bool(unconfirmed_replies(rows, observation))


def plan_targets(targets, existing, requirements, refresh=False):
    pending = {t["note_id"] for t in pending_targets(targets, existing, "xiaohongshu", "note_id", refresh=refresh)}
    for target in targets:
        key = target["note_id"]
        rows = requirements.get(key)
        if key in pending or not rows:
            continue
        if needs_reply_recheck(rows, existing[key]):
            pending.add(key)
    return [t for t in targets if t["note_id"] in pending]


def saved_observations(original, previous, existing):
    """Replace only rechecked XHS targets. Keep unrelated historical and cross-platform records."""
    changed = {key: value for key, value in existing.items() if previous.get(key) is not value}
    return [item for item in original if platform_key(item) != "xiaohongshu" or target_key(item) not in changed] + list(changed.values())


def open_comments_panel(page):
    panel = page.locator(".comments-container").first
    if panel.is_visible():
        return True
    button = page.get_by_role("button", name=re.compile(r"^(?:查看(?:全部)?评论|打开评论区|评论区)$")).first
    try:
        if button.is_visible():
            button.click(timeout=3000)
            panel.wait_for(state="visible", timeout=3000)
    except Exception:
        return False
    return panel.is_visible()


def reply_page_blocker(client):
    if login_gate_visible(client):
        return "login required during reply check"
    page = client._page
    dialogs = page.locator('[role="dialog"], .captcha-container, .verify-dialog, .security-dialog')
    message = dialogs.get_by_text(re.compile(r"请完成(?:安全)?验证|安全验证|验证码|访问(?:过于)?频繁")).first
    if message.is_visible():
        return "captcha or risk control during reply check"
    return ""


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
    extract = "items => items.map(item => { try { return (" + DOM_COMMENT_FIELDS + ")(item); } catch { return null; } })"
    try:
        records = items.evaluate_all(extract)
    except Exception:
        return [], visible_terminal(container), False
    for fields in records:
        if fields is None:
            extraction_complete = False
            continue
        comment = dom_comment_record(fields)
        if comment["text"] or comment.get("has_image") is True or comment.get("deleted"):
            comments.append(comment)
    return comments, visible_terminal(container), extraction_complete


def check_note_comments(page, key, raw_comments, declared, requirements, args, *, structured_ok=True, prior=None):
    captured = normalized_comments(raw_comments, root_list=True)
    observation = {"target_key": key, "platform": "xiaohongshu", "capture_status": "ok",
                   "capture_complete": structured_ok and not has_more(raw_comments) and (declared is None or len(captured) >= declared),
                   "comments": captured}
    missing = unconfirmed_replies(requirements, observation)
    page_needed = bool(missing or not structured_ok)
    expand = bool(page_needed or (args.expand_dom and not observation["capture_complete"]))
    terminal, parse_failed = False, False
    panel = open_comments_panel(page) if page_needed else None
    if expand and panel is not False:
        incoming, terminal, parsed = expand_dom_comments(page, args.dom_max_rounds)
        captured = prefer_comment_snapshot(captured, incoming)
        parse_failed = not parsed
        if (parse_failed or not dom_capture_complete(declared, len(captured), terminal)) and args.incomplete_dom_retry_rounds:
            incoming, end, parsed = expand_dom_comments(page, args.incomplete_dom_retry_rounds)
            captured = prefer_comment_snapshot(captured, incoming)
            terminal = terminal or end
            parse_failed = not parsed
    if panel is False:
        if args.expand_dom and declared is None:
            observation["capture_complete"] = False
    elif expand:
        observation["capture_complete"] = not parse_failed and dom_capture_complete(declared, len(captured), terminal)
    observation["comments"] = captured
    observation["image_detection_complete"] = not parse_failed
    if not structured_ok and (not panel or (not captured and not terminal)):
        observation.update(capture_status="inaccessible", capture_complete=False, image_detection_complete=False)
    if requirements:
        after = missing
        if expand and observation["capture_status"] == "ok":
            after = unconfirmed_replies(requirements, observation)
        signature = reply_signature(requirements)
        previous = (prior or {}).get("reply_verification", {})
        attempts = previous.get("attempt_count", int(bool(previous.get("page_attempted")))) if previous.get("requirements_sha256") == signature else 0
        if not page_needed:
            status = "not_needed"
        elif not panel:
            status = "panel_unavailable"
        else:
            status = "checked_unresolved" if after else "checked"
        observation["reply_verification"] = {
            "requirements_sha256": signature, "page_attempted": page_needed, "attempt_count": attempts + int(page_needed),
            "panel_available": panel, "unconfirmed_before": missing, "unconfirmed_after": after,
            "status": status, "structured_read": "ok" if structured_ok else "unavailable",
        }
    return observation, terminal, parse_failed


def collect_note_evidence(client, source_key, note_id, token, requirements, args, login_error, fetch_error, prior=None):
    def check_note_identity(check=None):
        active_note = target_from_url(str(client._page.url))
        if not active_note or active_note["note_id"] != note_id:
            error = fetch_error("note_identity_unconfirmed")
            if check:
                error.reply_verification = {**check, "status": "target_changed", "unconfirmed_after": None}
            raise error

    declared = None
    structured_ok = True
    try:
        declared = declared_comment_count(client.get_note_detail(note_id, token))
        raw = client.get_note_comments(note_id, token, max_comments=0)
    except login_error:
        raise
    except fetch_error as error:
        status = classify_failure(1, str(error))
        if status in ("session_expired", "captcha", "risk_control"):
            raise login_error(status if status != "session_expired" else "login required") from None
        if not requirements:
            raise
        # An unavailable data view can still leave the ordinary note page usable.
        structured_ok = False
        raw = []
    if login_gate_visible(client):
        raise login_error("login required: visible login gate")
    check_note_identity()
    if not structured_ok:
        blocker = reply_page_blocker(client)
        if blocker:
            raise login_error(blocker)
    evidence, terminal, parse_failed = check_note_comments(
        client._page, source_key, raw, declared, requirements, args, structured_ok=structured_ok, prior=prior)
    check = evidence.get("reply_verification", {})
    if check.get("page_attempted"):
        blocker = reply_page_blocker(client)
        if blocker:
            error = login_error(blocker)
            error.reply_verification = {**check, "status": "blocked", "unconfirmed_after": None}
            raise error
        check_note_identity(check)
    return evidence, terminal, parse_failed, declared


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True, help="Source XLSX; repeat for multiple files")
    parser.add_argument("--output", required=True)
    parser.add_argument("--verify-replies", action="append", default=[], metavar="EXPECTED_JSON",
                        help="Check unresolved required reply threads on the same note page")
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
        default=str(default_profile("xhs")),
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
    original = unwrap_rows(json.loads(output_path.read_text(encoding="utf-8")), "observations") if output_path.exists() else []
    previous = {target_key(item): item for item in original if platform_key(item) == "xiaohongshu"}
    existing = previous.copy()

    total_targets = len(targets)
    requirements = load_reply_requirements(args.verify_replies)
    targets = plan_targets(targets, existing, requirements, args.refresh)
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
            atomic_write(output_path, {"observations": saved_observations(original, previous, existing), "stopped": True,
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
                atomic_write(output_path, {"observations": saved_observations(original, previous, existing), "stopped": True, "stop_reason": stop_reason})
                print(f"[{index}/{len(targets)}] stop {note_id} status=network_changed", flush=True)
                break
            captured_at = datetime.now(timezone.utc).isoformat()
            target_started = time.monotonic()
            try:
                resolved_note_id, token = note_id, target["token"]
                source_url = target.get("source_url")
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
                    source_url = client._page.url
                client.use_source_note(resolved_note_id, source_url)
                evidence, dom_terminal, dom_parse_failed, declared = collect_note_evidence(
                    client, note_id, resolved_note_id, token, requirements.get(note_id), args, LoginError, DataFetchError,
                    None if args.refresh else existing.get(note_id))
                captured = evidence["comments"]
                incomplete = not evidence["capture_complete"]
                observation = {
                    **evidence,
                    "parser_revision": PARSER_REVISIONS["xiaohongshu"],
                    "resolved_target_key": resolved_note_id,
                    "captured_at": captured_at,
                    "declared_comment_count": declared,
                    "captured_comment_count": len(captured),
                    "pacing_profile": "risk-averse" if args.risk_averse else "standard",
                    "source_refs": target["sources"],
                }
                if incomplete:
                    observation["warnings"] = ["comment_dom_extraction_incomplete" if dom_parse_failed else "comments_pagination_incomplete"]
                elif dom_terminal and declared is not None and len(captured) < declared:
                    observation["warnings"] = ["visible_terminal_with_stale_declared_count"]
                existing[note_id] = observation
                processed_count += 1
                consecutive_errors = 0 if evidence["capture_status"] == "ok" else consecutive_errors + 1
                if consecutive_errors >= 2:
                    stop_reason = "repeated_navigation_failure"
                print(
                    f"[{index}/{len(targets)}] {evidence['capture_status']} {note_id} comments={len(captured)} complete={not incomplete}",
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
                if hasattr(exc, "reply_verification"):
                    existing[note_id]["reply_verification"] = exc.reply_verification
                stop_reason = status
                print(f"[{index}/{len(targets)}] stop {note_id} status={status}", flush=True)
            except DataFetchError as exc:
                existing[note_id] = {
                    "platform": "xiaohongshu", "target_key": note_id,
                    "capture_status": "inaccessible", "capture_complete": False,
                    "captured_at": captured_at, "warnings": ["note_data_unavailable"],
                    "comments": [], "source_refs": target["sources"],
                }
                if hasattr(exc, "reply_verification"):
                    existing[note_id]["reply_verification"] = exc.reply_verification
                print(f"[{index}/{len(targets)}] inaccessible {note_id}", flush=True)
                processed_count += 1
                consecutive_errors += 1
                if consecutive_errors >= 2:
                    stop_reason = "repeated_navigation_failure"
            existing[note_id]["capture_duration_ms"] = round((time.monotonic() - target_started) * 1000)
            atomic_write(output_path, {"observations": saved_observations(original, previous, existing), "stopped": bool(stop_reason), "stop_reason": stop_reason})
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
    configure_stdio()
    raise SystemExit(main())
