#!/usr/bin/env python3
"""Capture unique Xiaohongshu targets from XLSX files in one read-only browser session."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import re
import sys
import time
import zipfile
from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET

from xhs_capture import add_reply_relation, classify_failure, clean_comment_id, deleted_placeholder, flatten_comments, has_more, normalize_comment, normalized_comments
from capture_pacing import has_remaining_target, pause_seconds
from capture_watchdog import owner_confirmation_input, paced_sleep, visible_progress
from capture_resume import EMPTY_STATE_LABEL, EMPTY_STATE_SOURCE, fresh_capture, recheck_scope, PARSER_REVISIONS
from screenshot_evidence import NoteScreenshots, screenshots_valid
from audit_pass import rows_to_review
from review_comments import compare, platform_key, target_key, unwrap_rows
from xhs_persistent import persistent_client
from profile_lock import default_profile
from runtime_environment import configure_stdio
from session_environment import network_fingerprint
from xhs_visible_login import verification_status, wait_for_owner_confirmation
from table_to_expected import convert_xlsx_sheets, extract_url, parse_maps, read_worksheet, resolve_mapping, xlsx_shared_strings, xlsx_sheet_members


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
    if parsed.path.rstrip("/") == "/404":
        return None  # Its query names the failed request, not a readable note.
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


def task_link_cells(path, assigned, mapping):
    """Read the original share URL only in memory, from the same column as intake."""
    with zipfile.ZipFile(path) as archive:
        strings = xlsx_shared_strings(archive)
        for sheet, member in xlsx_sheet_members(archive).items():
            positions = assigned.get((Path(path).name, sheet))
            if not positions:
                continue
            rows = read_worksheet(archive, member, strings, target_url_header=mapping.get("target_url"))
            header = resolve_mapping(rows[0] if rows else (), mapping).get("target_url")
            current_url = ""
            for number, row in enumerate(rows, 2):
                value = str(row.get(header, "")).strip()
                if value:
                    current_url = extract_url(value)
                if number in positions:
                    key = positions[number]
                    if not header and re.fullmatch(r"[a-zA-Z0-9]+", key):
                        url = f"https://www.xiaohongshu.com/explore/{key}"
                    else:
                        url = current_url
                    target = target_from_url(url)
                    if not target or target["note_id"] != key:
                        raise ValueError(f"capture_target_identity_mismatch:{sheet}:{number}")
                    yield sheet, number, url


def capture_task_groups(paths, expected_paths, mapping):
    groups = [unwrap_rows(json.loads(Path(path).read_text(encoding="utf-8")), "rows") for path in expected_paths]
    prior = defaultdict(list)
    for group in groups:
        for row in group:
            if "prior_review" in row:
                prior[row.get("source_file")].append(row)
    discovered = {}
    for path in map(Path, paths):
        rows = convert_xlsx_sheets(path, mapping, None, skip_summary=True, skip_non_task_sheets=True)
        old = prior.get(path.name, [])
        if old or (rows and "prior_review" in rows[0]):
            # Match physical tasks and previous verdicts. Retain separately supplied price
            # rules in an up-to-date contract, but never select rows from a replaced workbook.
            def source_tasks(records):
                fields = ("source_sheet", "source_row", "platform", "target_url", "target_key", "comment_type",
                          "comment_text", "reply_to", "has_image", "image_requirement", "prior_review")
                return {r.get("case_id"): tuple(r.get(k) for k in fields) for r in records}
            if source_tasks(rows) != source_tasks(old):
                discovered[path.name] = rows
    if discovered:
        # First-pass JSON can predate the spreadsheet's results or a file rename.
        # In that case the workbook defines this pass and its physical source rows.
        remaining = {Path(path).name for path in paths} - discovered.keys()
        groups = [[r for r in group if r.get("source_file") in remaining] for group in groups]
        groups.extend(discovered.values())
    return groups


def targets_from_workbooks(paths: Iterable[str], *, task_rows=None, mapping=None, unfiltered_paths=()) -> List[Dict[str, Any]]:
    targets: Dict[str, Dict[str, Any]] = {}
    seen_sources: set[tuple[str, str, str, int]] = set()
    assigned = defaultdict(dict)
    if task_rows is not None:
        for row in task_rows:
            if platform_key(row) == "xiaohongshu":
                assigned[row["source_file"], row["source_sheet"]][row["source_row"]] = target_key(row)
    for input_path in paths:
        source_file = Path(input_path).name
        current_url_by_sheet: Dict[str, str] = {}
        cells = (workbook_column_a(input_path) if task_rows is None or input_path in unfiltered_paths
                 else task_link_cells(input_path, assigned, mapping or {}))
        for sheet_name, row_number, value in cells:
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
    scoped_sources = sum(1 for _, file, sheet, row in seen_sources if row in assigned.get((file, sheet), ()))
    if task_rows is not None and scoped_sources != sum(map(len, assigned.values())):
        raise ValueError("capture_plan_source_rows_missing")
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
        wait_for_owner_confirmation(client, REQUIRED_COOKIES, save_cookies, confirm=owner_confirmation_input)
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
    if (!item.getClientRects().length || getComputedStyle(item).visibility !== 'visible') return {hidden: true};
    const own = (element, selector) => [...element.querySelectorAll(selector)].filter(
        node => node.closest('.comment-item') === element);
    const commentId = element => element.getAttribute('data-comment-id') ||
        /^comment-([0-9a-f]{24})$/i.exec(element.id || '')?.[1] || null;
    const recipientLabel = '.reply-to, [data-reply-recipient-label]';
    const bodyText = element => {
        const emojis = [...element.querySelectorAll('img[alt]')].filter(img =>
            img.matches('.emoji, .emoji-img, .emoji-icon, [data-emoji]') ||
            /^\[[^\[\]]{1,24}\]$/.test(img.alt));
        if (!emojis.length && !element.querySelector(recipientLabel)) return element.innerText;
        const parts = [];
        const walk = node => {
            if (node instanceof Element && node.matches(recipientLabel)) return;
            if (node.nodeType === Node.TEXT_NODE) parts.push(node.textContent);
            else if (emojis.includes(node)) parts.push(node.alt);
            else if (node instanceof Element && getComputedStyle(node).display !== 'none') {
                if (node.tagName === 'BR') parts.push('\n');
                else node.childNodes.forEach(walk);
            }
        };
        walk(element);
        return parts.join('');
    };
    let bodies = own(item, '.content .note-text');
    if (!bodies.length) bodies = own(item, '.content').filter(node => !node.querySelector('.comment-item'));
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
    const content = own(item, '.content');
    const singleDisplayedReply = isReplyInGroup && group.querySelectorAll('.comment-item').length === 2 &&
        group.querySelectorAll(':scope > .reply-container').length === 1 &&
        group.querySelectorAll(':scope > .reply-container > .list-container').length === 1 &&
        item.matches('.reply-container > .list-container > .comment-item-sub') &&
        roots[0].getClientRects().length && getComputedStyle(roots[0]).visibility === 'visible' &&
        rootBodies.length === 1 && rootBodies[0].innerText.trim() &&
        content.length === 1 && bodies.length === 1 && content[0].childElementCount === 1 &&
        content[0].firstElementChild === bodies[0] && bodies[0].matches('span.note-text') &&
        content[0].innerText.trim() === bodies[0].innerText.trim() &&
        !/^回复(?:\s*@|\s*.{0,100}[:：])/s.test(bodies[0].innerText.trim()) &&
        !group.querySelector('[data-reply-to], .reply-to, .reply-user, .mention, .at-user, [aria-expanded="false"], .folded, .deleted');
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
            if (texts.length === 1) parentText = bodyText(texts[0]).trim();
        }
    }
    return {
        text: ownText ? bodyText(bodies[0]) : item.innerText,
        own_text: ownText,
        reply_label_removed: ownText && content.some(node => node.contains(bodies[0]) &&
            [...node.querySelectorAll(recipientLabel)].some(label => label.closest('.comment-item') === item)),
        has_image: own(item, '.comment-picture img').length > 0,
        comment_id: commentId(item),
        parent_comment_id: parentId,
        target_comment_id: targetId,
        parent_text: parentText,
        thread_root_text: rootBodies.length === 1 ? rootBodies[0].innerText.trim() : null,
        single_displayed_root: singleDisplayedReply && !directId && !targetId ?
            {text: bodyText(rootBodies[0]).trim(), comment_id: commentId(roots[0])} : null
    };
}"""


def dom_comment_record(fields: Dict[str, Any]) -> Dict[str, Any]:
    result = {"text": fields["text"].strip(), "has_image": fields["has_image"]}
    if fields.get("reply_label_removed"):
        result["reply_label_removed"] = True
    identifier = clean_comment_id(fields.get("comment_id"))
    if identifier:
        result["comment_id"] = identifier
    add_reply_relation(result, fields, root_list=False)
    if fields.get("thread_root_text") and deleted_placeholder(fields["thread_root_text"]):
        result["thread_root_deleted"] = True
    candidate = fields.get("single_displayed_root")
    if candidate and not deleted_placeholder(candidate["text"]):
        result["_displayed_root"] = candidate
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


def apply_displayed_parents(comments, raw, complete):
    """Attribute only the sole displayed reply of a complete, unambiguous thread."""
    pending = []
    for child in comments:
        root = child.pop("_displayed_root", None)
        if (complete and root and not child.get("deleted") and not child.get("thread_root_deleted")
                and "parent_comment_id" not in child and not child.get("parent_text")):
            pending.append((child, root))
    if not pending:
        return

    roots = defaultdict(list)
    members = defaultdict(list)
    def identifiers(record):
        return [(key, record[key]) for key in ("comment_id", "text") if record.get(key)]
    for thread in raw:
        if not isinstance(thread, dict):
            continue
        entries = [(normalize_comment(r), r) for r in flatten_comments([thread])]
        first = entries[0][0]
        for key in identifiers(first):
            roots[key].append(entries)
        for index, (record, source) in enumerate(entries):
            for key in identifiers(record):
                members[key].append((record, first, index, source))

    def same(left, right):
        if left.get("comment_id") and right.get("comment_id"):
            return left["comment_id"] == right["comment_id"]
        return bool(left.get("text") and left["text"] == right.get("text"))

    directed_keys = ("target_comment", "targetComment", "parent_comment", "parentComment", "parent_comment_id",
                     "parentCommentId", "target_comment_id", "targetCommentId", "reply_to_user", "replyToUser",
                     "target_user", "targetUser", "target_user_id", "targetUserId")
    def disagrees(child, root):
        for key in identifiers(root):
            for entries in roots.get(key, ()):
                first = entries[0][0]
                if same(first, root) and (first.get("deleted") or len(entries) > 2 or first["text"] != root["text"]
                        or (len(entries) == 2 and not same(entries[1][0], child))):
                    return True
        for key in identifiers(child):
            for record, first, index, source in members.get(key, ()):
                if same(record, child) and (index == 0 or not same(first, root) or any(source.get(k) for k in directed_keys)):
                    return True
        return False

    for child, root in pending:
        if not disagrees(child, root):
            child.update(parent_text=root["text"], parent_evidence="single_visible_reply_under_root")


def prefer_comment_snapshot(current, incoming, *, visible_complete=False):
    """Preserve distinct observed comments without multiplying anonymous copies across views."""
    if not incoming:
        return [] if visible_complete else current
    if not current:
        return incoming
    def quality(records):
        links = sum(bool(c.get("parent_comment_id") or c.get("parent_text")) for c in records)
        return (len(records), links, sum("comment_id" in c for c in records),
                sum(c.get("thread_root_deleted") is True for c in records))
    primary, secondary = (incoming, current) if visible_complete or quality(incoming) > quality(current) else (current, incoming)
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
        if (primary is current and other and counts[identifier] == 1
                and (other.get("text_source") == "dom_note_text" or other.get("deleted"))
                and (other["text"], other["has_image"]) != (comment["text"], comment["has_image"])):
            comment = other
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
    if visible_complete:
        return selected
    # Reserve known ID matches first. An anonymous appearance can overlap one
    # record with the same body and image, never two different known IDs.
    def fingerprint(item):
        return item["text"], item["has_image"]
    shared_ids = set(counts).intersection(known) - {None}
    available = Counter(fingerprint(c) for c in primary if c.get("comment_id") not in shared_ids)
    anonymous = Counter(fingerprint(c) for c in primary if not c.get("comment_id"))
    known_texts = {c["text"] for c in primary}
    anonymous_texts = {c["text"] for c in primary if not c.get("comment_id")}
    for other in sorted(secondary, key=lambda item: not bool(item.get("comment_id"))):
        identifier = other.get("comment_id")
        if identifier in shared_ids:
            continue
        key = fingerprint(other)
        capacity = anonymous if identifier else available
        if capacity[key] > 0:
            available[key] -= 1
            if identifier:
                anonymous[key] -= 1
        elif (key not in available and other["text"] in (anonymous_texts if identifier else known_texts)):
            # An image-state difference on an unidentified copy of the same body
            # cannot establish that two separate comments were published.
            continue
        else:
            selected.append(other)
    return selected


MORE_REPLIES = re.compile(r"^\s*(?:展开\s*(?:剩余\s*)?(?:\d+|更多)\s*条?\s*(?:回复|评论)|(?:查看|加载)更多(?:回复|评论)|更多回复|查看(?:被)?折叠(?:的)?评论)\s*$")
END_TEXT = re.compile(r"^\s*(?:-\s*THE END\s*-|没有更多评论|暂时没有更多评论)\s*$", re.I)
EMPTY_TEXT = re.compile(r"^\s*(?:暂无评论|(?:暂时)?还没有评论(?:哦)?|还没有人评论)(?:[～~。！!])?\s*$")
OUTSIDE_CARDS = ':visible:not(.comment-item):not(.comment-item *):not(.reply-container):not(.reply-container *)'


def reply_controls(container):
    return container.get_by_text(MORE_REPLIES).and_(container.locator(
        ':visible:not(.content):not(.content *):not(.note-text):not(.note-text *)'))


def visible_terminal(container: Any) -> bool:
    # A footer can remain mounted while the panel or a reply group is loading.
    if container.evaluate("""panel => panel.getAttribute('aria-busy') === 'true' ||
        [...panel.querySelectorAll('[aria-busy="true"], [role="progressbar"], .comments-loading, .reply-loading, .loading-more, .loading-container, .loading')]
        .some(node => !node.closest('.content, .note-text, .comment-picture') &&
            node.getClientRects().length && getComputedStyle(node).visibility === 'visible')"""):
        return False
    # A comment (or reply thread) can itself contain the same words as a footer.
    outside_comments = container.locator(OUTSIDE_CARDS)
    if container.get_by_text(END_TEXT).and_(outside_comments).count():
        return True
    return (not container.locator(".comment-item:visible").count()
            and container.get_by_text(EMPTY_TEXT).and_(outside_comments).count() > 0)


def group_note_requirements(rows):
    """Check all assigned rows before reusing a partial observation for positive evidence."""
    groups = {}
    for row in rows:
        if platform_key(row) == "xiaohongshu":
            groups.setdefault(target_key(row), []).append(row)
    return groups


def reply_signature(rows, context_rows=None):
    signature_data = rows if context_rows is None else {"tasks": rows, "context": context_rows}
    return hashlib.sha256(json.dumps(signature_data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def unconfirmed_replies(rows, observation, context_rows=None):
    if not rows:
        return 0
    results = compare(rows, [observation], amount_mode="expected-only", require_reply_parent=True,
                      reply_context_rows=context_rows)["results"]
    reasons = {"reply_parent_unconfirmed", "reply_source_parent_ambiguous"}
    return sum(bool(reasons.intersection(row["reason_codes"])) for row in results)


def needs_reply_recheck(rows, observation, context_rows=None):
    """Decide whether a fresh checkpoint still needs a same-page check for these source rows."""
    if not rows:
        return False
    if not observation or observation.get("capture_status") != "ok":
        return True
    check = observation.get("reply_verification") or {}
    if check.get("page_attempted") and check.get("requirements_sha256") == reply_signature(rows, context_rows):
        if check.get("panel_available") or check.get("attempt_count", 1) >= 2:
            return False
    return bool(unconfirmed_replies(rows, observation, context_rows))


def capture_covers_tasks(observation, key, rows, *, require_reply_parent=False, context_rows=None, screenshot_dir=None):
    if not fresh_capture(observation, "xiaohongshu", key):
        return False
    if screenshot_dir is not None and not screenshots_valid(observation, screenshot_dir):
        return False
    if observation.get("capture_complete") is True:
        if require_reply_parent:
            return not needs_reply_recheck(rows, observation, context_rows)
        if (rows and observation.get("reply_label_reader") != "dom-markers-v1"
                and any(str(c.get("text", c.get("comment_text", ""))).lstrip().startswith(("回复", "回覆", "@", "＠"))
                        for c in observation["comments"])):
            # Older readers could include an explicitly labelled recipient in the body.
            # Only reopen when it actually leaves a task unresolved on this note.
            results = compare(rows, [observation], amount_mode="expected-only", reply_context_rows=context_rows)["results"]
            return not any("reply_body_prefix_unconfirmed" in result["reason_codes"] for result in results)
        return True
    if not rows or require_reply_parent or observation.get("capture_complete") is not False:
        return False
    # An unfinished list can prove individual appearances. It cannot prove an
    # absence, missing image, partial payment, or visibility on another note.
    results = compare(rows, [observation], amount_mode="expected-only", reply_context_rows=context_rows)["results"]
    if observation.get("capture_stop_reason") == "recheck_matches_visible":
        return settled_recheck_matches(results)
    return all(result["status"] == "verified" for result in results)


def settled_recheck_matches(results):
    # A full-price match with an independent, identifiable comment cannot earn
    # more from another page. An unattached image or a shared claim still can.
    return bool(results) and all(
        r["expected_type"] in ("text", "text+image") and r["status"] == "verified"
        and r.get("approved_amount") is not None and not r.get("settlement_status")
        and clean_comment_id(r.get("matched_comment", {}).get("comment_id")) for r in results
    )


def plan_targets(targets, existing, requirements, refresh=False, *, require_reply_parent=False, contexts=None, screenshot_dir=None):
    if refresh:
        return list(targets)
    return [target for target in targets if not capture_covers_tasks(
        existing.get(target["note_id"]), target["note_id"], requirements.get(target["note_id"]),
        require_reply_parent=require_reply_parent, context_rows=(contexts or {}).get(target["note_id"]), screenshot_dir=screenshot_dir)]


def saved_observations(original, previous, existing):
    """Replace only rechecked XHS targets. Keep unrelated historical and cross-platform records."""
    changed = {key: value for key, value in existing.items() if previous.get(key) is not value}
    return [item for item in original if platform_key(item) != "xiaohongshu" or target_key(item) not in changed] + list(changed.values())


def open_comments_panel(page):
    panel = page.locator(".comments-container:visible").first
    if panel.is_visible():
        return True
    button = page.get_by_role("button", name=re.compile(r"^(?:查看(?:全部)?评论|打开评论区|评论区)$")).first
    try:
        # Either surface can be mounted after DOMContentLoaded.
        panel.or_(button).first.wait_for(state="visible", timeout=3500)
        if not panel.is_visible() and button.is_visible():
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


def read_visible_comments(container, photos=None):
    items = container.locator(".comment-item")
    extract = "items => items.map(item => { try { return (" + DOM_COMMENT_FIELDS + ")(item); } catch { return null; } })"
    try:
        records = items.evaluate_all(extract)
    except Exception:
        return [], False
    comments = []
    positioned = []
    identities = {}
    parsed = True
    for index, fields in enumerate(records):
        if fields is None:
            parsed = False
            continue
        if fields.get("hidden"):
            continue
        comment = dom_comment_record(fields)
        if not (comment["text"] or comment.get("has_image") is True or comment.get("deleted")):
            parsed = False
            continue
        identifier = comment.get("comment_id")
        if identifier:
            if identifier in identities:
                if identities[identifier] != comment:
                    parsed = False
                    identities[identifier] = None
                continue
            identities[identifier] = comment
        comments.append(comment)
        if photos is not None:
            positioned.append((index, comment))
    if photos is not None:
        photos.cards(container, items, [(i, c) for i, c in positioned
                                      if not c.get("comment_id") or identities[c["comment_id"]] is not None])
    return [c for c in comments if not c.get("comment_id") or identities[c["comment_id"]] is not None], parsed


def displayed_identities(records):
    return Counter(c.get("comment_id") or (c["text"], c["has_image"]) for c in records)


def has_visible_comment(records):
    return any(not c.get("deleted") and (str(c.get("text") or "").strip() or c.get("has_image") is True)
               for c in records)


class CommentPhotos:
    """Capture rendered cards before scrolling can recycle them; associate only stable pixels with an observation."""

    def __init__(self, writer, page, note_id):
        self.writer = writer
        self.page = page
        self.note_id = note_id
        self.by_card = {}
        self.end = None
        self.waited_for_images = set()

    @staticmethod
    def key(comment):
        return (comment.get("comment_id"), comment.get("text"), comment.get("has_image"), bool(comment.get("deleted")))

    def same_note(self):
        target = target_from_url(str(self.page.url))
        return target and target["note_id"] == self.note_id and note_page_state(self.page, self.note_id) != "wrong_note"

    def take(self, node, kind, *, valid, ids=(), image_keys=()):
        record = None
        try:
            # Responsive note layouts can keep Playwright's action-stability wait pending
            # even when the correct card already fills a stationary, visible rectangle.
            exposed = """e => {
                const r = e.getBoundingClientRect();
                const x = (Math.max(0,r.left) + Math.min(innerWidth,r.right))/2;
                const y = (Math.max(0,r.top) + Math.min(innerHeight,r.bottom))/2;
                const hit = document.elementFromPoint(x,y);
                return !!hit && (e.contains(hit) ||
                    (getComputedStyle(e).pointerEvents === 'none' && hit.contains(e)));
            }"""
            node.evaluate("""e => {
                const box = e.getBoundingClientRect();
                let top = 0, left = 0, right = innerWidth, bottom = innerHeight;
                for (let p = e.parentElement; p; p = p.parentElement) {
                    const css = getComputedStyle(p), r = p.getBoundingClientRect();
                    if (/(auto|scroll|hidden|clip)/.test(css.overflowY)) { top=Math.max(top,r.top); bottom=Math.min(bottom,r.bottom); }
                    if (/(auto|scroll|hidden|clip)/.test(css.overflowX)) { left=Math.max(left,r.left); right=Math.min(right,r.right); }
                }
                const hit = document.elementFromPoint((box.left+box.right)/2,(box.top+box.bottom)/2);
                if (box.top < top || box.bottom > bottom || box.left < left || box.right > right ||
                    !hit || !(e.contains(hit) || (getComputedStyle(e).pointerEvents === 'none' && hit.contains(e))))
                    e.scrollIntoView({block:'center', inline:'nearest', behavior:'instant'});
            }""")
            image_ready = """async (node, wait) => {
                const end = performance.now() + wait;
                do {
                    const images = [...node.querySelectorAll('.comment-picture img')];
                    if (images.length && images.every(i => i.complete && i.naturalWidth && i.naturalHeight &&
                            i.getClientRects().length && getComputedStyle(i).visibility === 'visible')) return true;
                    if (performance.now() >= end) return false;
                    await new Promise(done => setTimeout(done, 40));
                } while (node.isConnected);
                return false;
            }"""
            # Lazy thumbnails can start loading only when the card is scrolled into view.
            wait = 800 if any(k not in self.waited_for_images for k in image_keys) else 0
            self.waited_for_images.update(image_keys)
            if self.same_note() and node.evaluate(exposed) and valid() and (not image_keys or node.evaluate(image_ready, wait)):
                record = self.writer.save(node, kind, comment_ids=ids)
                if record and self.same_note() and node.evaluate(exposed) and valid() and (not image_keys or node.evaluate(image_ready, 0)):
                    return record
        except Exception:
            # A recycled or detached node still needs a new, verifiable screenshot.
            pass
        if record:
            self.writer.discard(record)
        return None

    def card(self, node, comment):
        key = self.key(comment)
        if key in self.by_card:
            return
        def matches():
            fields = node.evaluate(DOM_COMMENT_FIELDS)
            return bool(fields and not fields.get("hidden") and self.key(dom_comment_record(fields)) == key)
        record = self.take(node, "comment", valid=matches, ids=[key[0]] if key[0] else [],
                           image_keys=[key] if key[2] else [])
        if not record and key[1] and not key[2] and comment.get("text_source") == "dom_note_text":
            # A floating bar can cover a card's center while its actual comment body is fully visible.
            handle = None
            try:
                handle = node.evaluate_handle("""card => {
                    const own = [...card.querySelectorAll('.content .note-text')]
                        .filter(e => e.closest('.comment-item') === card && e.getClientRects().length);
                    return own.length === 1 ? own[0] : null;
                }""")
                body = handle.as_element()
                if body is not None:
                    record = self.take(body, "comment", valid=matches, ids=[key[0]] if key[0] else [])
            except Exception:
                pass
            finally:
                if handle is not None:
                    handle.dispose()
        if record:
            if key[1]:
                record["comment_texts"] = [key[1]]
            self.by_card[key] = record
            visible_progress()

    def cards(self, panel, items, positioned):
        keys = [self.key(c) for _, c in positioned]
        if not keys or all(key in self.by_card for key in keys):
            return
        try:
            compact = panel.evaluate("""p => {
                const bounds = p.getBoundingClientRect();
                let height = innerHeight, width = innerWidth;
                for (let e = p; e; e = e.parentElement) {
                    const css = getComputedStyle(e);
                    if (/(auto|scroll|hidden|clip)/.test(css.overflowY)) height = Math.min(height,e.clientHeight);
                    if (/(auto|scroll|hidden|clip)/.test(css.overflowX)) width = Math.min(width,e.clientWidth);
                }
                return bounds.height <= height && bounds.width <= width && p.scrollHeight <= p.clientHeight + 1;
            }""")
            if compact:
                at_end = visible_terminal(panel) and not reply_controls(panel).count()
                def same_screen():
                    states, parsed = read_visible_comments(panel)
                    return (parsed and [self.key(c) for c in states] == keys
                            and (not at_end or visible_terminal(panel) and not reply_controls(panel).count())
                            and panel.evaluate("""p => {
                                const box = p.getBoundingClientRect();
                                const visible = (x,y) => p.contains(document.elementFromPoint(x,y));
                                return box.width > 2 && box.height > 2 && visible(box.left+2,box.top+2)
                                    && visible(box.right-2,box.bottom-2);
                            }"""))
                record = self.take(panel, "end" if at_end else "comment", valid=same_screen,
                                   ids=[k[0] for k in keys if k[0]], image_keys=[k for k in keys if k[2]])
                if record:
                    record["comment_texts"] = [k[1] for k in keys if k[1]]
                    self.by_card.update((k, record) for k in keys)
                    if at_end:
                        self.end = record
                    visible_progress()
                    return
        except Exception:
            pass
        for i, comment in positioned:
            self.card(items.nth(i), comment)

    def finish(self, observation):
        panel = self.page.locator(".comments-container:visible").first
        if observation.get("capture_complete"):
            if observation.get("capture_complete_source") == EMPTY_STATE_SOURCE:
                empty = self.page.locator(".note-container .comments-el .no-comments:visible").first
                def still_empty():
                    return ("".join(empty.inner_text(timeout=1200).split()) == "这是一片荒地点击评论"
                            and not panel.count() and note_page_state(self.page, self.note_id) == "target_note")
                self.end = self.take(empty, "empty", valid=still_empty)
                if not self.end:
                    self.end = self.take(empty.locator(".no-comments-text:visible").first, "empty", valid=still_empty)
            elif not self.end and panel.count():
                outside = panel.locator(OUTSIDE_CARDS)
                footer = panel.get_by_text(END_TEXT).and_(outside).first
                kind = "end"
                if not footer.count():
                    footer = panel.get_by_text(EMPTY_TEXT).and_(outside).first
                    kind = "empty"
                if footer.count():
                    self.end = self.take(footer, kind, valid=lambda: visible_terminal(panel) and not reply_controls(panel).count())
        complete = (bool(self.end) if observation.get("capture_complete")
                    else observation.get("capture_stop_reason") == "recheck_matches_visible")
        for comment in observation.get("comments", []):
            shot = self.by_card.get(self.key(comment))
            if shot:
                comment["evidence_path"] = shot["path"]
            else:
                complete = False
        return self.attach(observation, complete and observation.get("capture_status") == "ok")

    def attach(self, observation, complete):
        records = self.writer.records
        observation["comment_screenshots"] = records
        # A failed panel-level shot can be fully recovered by individual cards plus a footer.
        observation["screenshot_status"] = ("missing" if not records else
                                            "captured" if complete else "partial")
        return observation


class ExpansionChecks(ExitStack):
    """A disappearing button and an old footer cannot confirm async replies arrived."""

    def __init__(self, sufficient=None, photos=None):
        super().__init__()
        self.pending = []
        self.sufficient = sufficient
        self.photos = photos
        self.stopped_on_matches = False

    def read(self, container):
        if self.photos is not None:
            return read_visible_comments(container, self.photos)
        return read_visible_comments(container)

    def all_matches_visible(self, comments):
        if self.sufficient and self.sufficient(comments):
            self.stopped_on_matches = True
        return self.stopped_on_matches

    def click(self, page, control):
        # An unchanged control may still be loading after a timeout. Let that
        # request finish instead of toggling the same control again.
        if any(control.evaluate("(node, s) => node === s.control && s.added() <= 0", receipt)
               for receipt in self.pending):
            return
        receipt = control.evaluate_handle(r"""control => {
            const scope = control.closest('.parent-comment, .comment-item');
            const label = control.innerText.trim();
            const listPager = /^(?:查看|加载)更多评论$/.test(label);
            const group = scope?.closest('.parent-comment') || scope || (listPager && control.closest('.comments-container'));
            const count = () => {
                if (!group || !group.isConnected) return -1;
                const identities = new Set();
                for (const row of group.querySelectorAll('.comment-item')) {
                    if (row.getClientRects().length && getComputedStyle(row).visibility === 'visible')
                        identities.add(row.getAttribute('data-comment-id') || row.id || row);
                }
                return identities.size;
            };
            const before = count();
            const required = Number(label.match(/\d+/)?.[0]) || 1;
            return {control, added: () => count() - before, required, started: performance.now()};
        }""")
        self.callback(receipt.dispose)
        self.pending.append(receipt)
        control.click(timeout=3000)
        # A partial page can expose the next button before the full advertised
        # quantity arrives. Keep that obligation through the same-page retry.
        page.wait_for_function("s => s.added() > 0", arg=receipt, timeout=3500).dispose()
        visible_progress()
        if receipt.evaluate("s => s.added() >= s.required"):
            self.pending.remove(receipt)

    def confirmed(self, page):
        for receipt in self.pending:
            # Numbered replies may appear in consecutive renders. All checks
            # share their original deadline, including across an optional retry.
            remaining = receipt.evaluate("s => Math.ceil(3500 - (performance.now() - s.started))")
            if remaining > 0:
                try:
                    page.wait_for_function("s => s.added() >= s.required", arg=receipt, timeout=remaining).dispose()
                except Exception:
                    pass
            if not receipt.evaluate("s => s.added() >= s.required"):
                return False
        self.pending.clear()
        return True


def expand_dom_comments(page: Any, max_rounds: int = 30, *, checks=None) -> tuple[List[Dict[str, Any]], bool, bool]:
    """Expand reply groups and lazy-load comments without social interactions."""
    if checks is None:
        with ExpansionChecks() as local_checks:
            return expand_dom_comments(page, max_rounds, checks=local_checks)
    container = page.locator(".comments-container:visible").first
    if not container.count():
        return [], False, False
    stable = 0
    previous = None
    seen = []
    parse_gap = False
    for _ in range(max_rounds):
        expanders = reply_controls(container)
        if not expanders.count() and visible_terminal(container):
            break
        # Read before scrolling because a virtualized list may recycle its earlier nodes.
        rendered, parsed = checks.read(container)
        parse_gap |= not parsed
        if not parse_gap and checks.all_matches_visible(rendered):
            # This snapshot proves the selected successes, not the rest of the list.
            return rendered, False, True
        previous_count = len(seen)
        seen = prefer_comment_snapshot(seen, rendered)
        if len(seen) > previous_count:
            visible_progress()
        state = (displayed_identities(rendered), expanders.count())
        stable = stable + 1 if state == previous else 0
        previous = state
        if stable >= 4:
            break
        # Clicking can remove the node. Reverse order avoids skipping a sibling.
        for index in reversed(range(expanders.count())):
            expander = expanders.nth(index)
            try:
                if expander.is_visible():
                    expander.scroll_into_view_if_needed(timeout=2000)
                    checks.click(page, expander)
            except Exception:
                continue
        if visible_terminal(container) and not reply_controls(container).count():
            break
        items = container.locator(".comment-item")
        count = items.count()
        if count:
            try:
                items.last.scroll_into_view_if_needed(timeout=3000)
                page.mouse.wheel(0, 600)
            except Exception:
                pass
        page.wait_for_timeout(random.randint(800, 1300))
        terminal = visible_terminal(container)
        if terminal and not reply_controls(container).count():
            break
    if checks.sufficient and checks.pending and not parse_gap:
        rendered, parsed = checks.read(container)
        if parsed and checks.all_matches_visible(rendered):
            return rendered, False, True
    terminal = visible_terminal(container) and not reply_controls(container).count() and checks.confirmed(page)
    # Rows and a fresh pagination control may have appeared during confirmation.
    rendered, parsed = checks.read(container)
    parsed = parsed and not parse_gap
    terminal = terminal and visible_terminal(container) and not reply_controls(container).count()
    if displayed_identities(seen) - displayed_identities(rendered):
        # Some previously displayed records disappeared mid-traversal. They were
        # observed, but a rendered footer cannot prove we saw every intervening row.
        return prefer_comment_snapshot(seen, rendered), False, parsed
    return rendered, terminal, parsed


def stable_visible_empty_state(page, note_id, raw_comments, declared):
    """Recognize the separate, note-bound empty component after giving lazy content a chance to mount."""
    active = target_from_url(str(getattr(page, "url", "")))
    if not active or active["note_id"] != note_id or has_more(raw_comments):
        return False
    inspect = r"""({expected, scroll, more}) => {
        const visible = e => e.getClientRects().length && getComputedStyle(e).visibility === 'visible';
        const shown = (root, selector) => [...root.querySelectorAll(selector)].filter(visible);
        const detail = window.__INITIAL_STATE__?.note?.noteDetailMap?.[expected], note = detail?.note;
        if (String(note?.noteId || note?.note_id || note?.id || '') !== expected) return false;
        const notes = shown(document, '.note-container');
        if (notes.length !== 1 || shown(document, '.comments-container').length) return false;
        const root = notes[0], areas = shown(root, '.interaction-container .note-scroller .comments-el');
        if (areas.length !== 1) return false;
        const area = areas[0], empty = shown(area, '.no-comments');
        if (empty.length !== 1 || !shown(empty[0], '.no-comments-image').length) return false;
        const labels = shown(empty[0], '.no-comments-text'), compact = s => s.replace(/\s+/g, '');
        const wording = '这是一片荒地点击评论';
        if (labels.length !== 1 || compact(labels[0].innerText) !== wording || compact(area.innerText) !== wording) return false;
        // The dependency returns only the list and can discard its outer pagination flags.
        const containers = [detail.comments], seen = new Set();
        while (containers.length) {
            const value = containers.pop();
            if (!value || typeof value !== 'object' || Array.isArray(value) || seen.has(value)) continue;
            seen.add(value);
            if ([value.hasMore, value.has_more].some(v => v === true || v === 1 || v === '1' || v === 'true')) return false;
            containers.push(value.comments, value.data, value.list, value.pageInfo, value.pagination);
        }
        const scroller = area.closest('.note-scroller');
        if (scroller.querySelector('.comment-item, [data-comment-id], .reply-container, .folded, .comments-container')
                || area.closest('[aria-busy="true"]') || shown(root,
                    '[aria-busy="true"], [role=progressbar], .comments-loading, .reply-loading, .loading-more, .loading-container, .loading, .skeleton').length)
            return false;
        const pending = new RegExp(more);
        if (shown(scroller, 'button, [role=button], a, span, [aria-expanded=false]').some(e => pending.test(e.innerText))) return false;
        const restrictions = /(?:评论(?:已关闭|暂时不可见|仅对.*可见)|暂时无法查看评论|仅作者可见|作者已关闭评论)/;
        if (shown(root, '[role=alert], [class*="notice"], [class*="restrict"], [class*="error"], [class*="permission"]')
                .some(e => !e.closest('.desc, .note-content') && restrictions.test(e.innerText))) return false;
        if (scroll) {
            labels[0].scrollIntoView({block:'center', inline:'nearest', behavior:'instant'});
        } else {
            const rect = labels[0].getBoundingClientRect(), x = rect.left + rect.width/2, y = rect.top + rect.height/2;
            const hit = document.elementFromPoint(x, y);
            if (!hit || (!empty[0].contains(hit) && hit !== area)) return false;
        }
        return true;
    }"""
    def matches(scroll):
        return (page.evaluate(inspect, {"expected": note_id, "scroll": scroll, "more": MORE_REPLIES.pattern})
                and note_page_state(page, note_id) == "target_note")
    if not matches(True):
        return False
    page.wait_for_timeout(1000 if declared and declared > 0 else 650)
    active = target_from_url(str(page.url))
    return bool(active and active["note_id"] == note_id and matches(False))


def check_note_comments(page, key, raw_comments, declared, requirements, args, *, structured_ok=True, prior=None,
                        context_rows=None, resolved_note_id=None, photos=None):
    captured = normalized_comments(raw_comments, root_list=True)
    observation = {"target_key": key, "platform": "xiaohongshu", "capture_status": "ok",
                   "capture_complete": False, "comments": captured}
    strict = getattr(args, "require_reply_parent", False)
    missing = unconfirmed_replies(requirements, observation, context_rows) if strict else 0
    page_needed = bool(missing or not structured_ok)
    expand = bool(page_needed or args.expand_dom or (requirements and not strict))
    terminal, parse_failed = False, False
    note_id = resolved_note_id or key
    empty_state = stable_visible_empty_state(page, note_id, raw_comments, declared)
    panel = empty_state or open_comments_panel(page)
    if not panel:
        # The separately rendered empty area can mount during the ordinary panel wait.
        empty_state = stable_visible_empty_state(page, note_id, raw_comments, declared)
        panel = empty_state
    if empty_state:
        captured, terminal = [], True
        observation.update(visible_comment_panel_readable=True, target_page_state="target_note",
                           empty_state_label=EMPTY_STATE_LABEL, captured_comment_count=0)
        if declared and declared > 0:
            observation["warnings"] = ["visible_terminal_with_stale_declared_count"]
    elif panel:
        sufficient = None
        if not strict and requirements and all(r.get("prior_review", {}).get("triage") == "成功" for r in requirements):
            def sufficient(visible):
                if not visible:
                    return False
                partial = {**observation, "comments": visible, "reply_label_reader": "dom-markers-v1"}
                results = compare(requirements, [partial], amount_mode="expected-only", reply_context_rows=context_rows)["results"]
                return settled_recheck_matches(results)
        with ExpansionChecks(sufficient, photos) as checks:
            incoming, terminal, parsed = expand_dom_comments(page, args.dom_max_rounds if expand else 0, checks=checks)
            panel_readable = has_visible_comment(incoming)
            if expand and not checks.stopped_on_matches and (not parsed or not terminal) and args.incomplete_dom_retry_rounds:
                first_render = incoming
                first_view = displayed_identities(incoming)
                incoming, end, parsed = expand_dom_comments(page, args.incomplete_dom_retry_rounds, checks=checks)
                panel_readable = panel_readable or has_visible_comment(incoming)
                terminal = end and not (first_view - displayed_identities(incoming))
                if not terminal and not checks.stopped_on_matches:
                    incoming = prefer_comment_snapshot(first_render, incoming)
            if panel_readable:
                observation["visible_comment_panel_readable"] = True
            parse_failed = not parsed
            if checks.stopped_on_matches:
                captured = incoming
                observation["capture_stop_reason"] = "recheck_matches_visible"
            else:
                # Initial page state can contain comments that aren't displayed (including
                # deleted/hidden replies). Enrich matching rendered IDs, never append cached rows.
                captured = prefer_comment_snapshot(captured, incoming, visible_complete=True)
                if not terminal and checks.pending:
                    observation["warnings"] = ["reply_expansion_unconfirmed"]
    else:
        captured = []
        observation["warnings"] = ["comments_panel_unavailable"]
    observation["capture_complete"] = bool(panel and terminal and not parse_failed and not has_more(raw_comments))
    if panel and not parse_failed:
        observation["reply_label_reader"] = "dom-markers-v1"
    apply_displayed_parents(captured, raw_comments, observation["capture_complete"])
    if observation["capture_complete"]:
        observation["capture_complete_source"] = EMPTY_STATE_SOURCE if empty_state else "visible_comment_list_end"
    observation["comments"] = captured
    observation["image_detection_complete"] = bool(panel and not parse_failed)
    if not panel or (not structured_ok and not captured and not terminal):
        observation.update(capture_status="inaccessible", capture_complete=False, image_detection_complete=False)
    if strict and requirements:
        after = missing
        if observation["capture_status"] == "ok":
            after = unconfirmed_replies(requirements, observation, context_rows)
        signature = reply_signature(requirements, context_rows)
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
    if photos is not None:
        photos.finish(observation)
    return observation, terminal, parse_failed


SINGLE_NOTE_PAGE_STATES = ("target_note", "post_unavailable")

NOTE_ERROR_TEXT = re.compile(
    r"^\s*(?:抱歉[，,！!\s]*)?(?:你访问的页面不见了|你访问的笔记不见了|(?:这篇|该|当前)?笔记(?:已被删除|已删除|不存在|暂时无法浏览|暂时无法查看)"
    r"|(?:该|当前)内容(?:不存在|暂时无法查看|暂时无法浏览|不可见))[。！!～~]?\s*$")


def official_note_error_url(url, note_id):
    """The note-specific 404 has a signed-in official origin and one explicit target."""
    try:
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.hostname != "www.xiaohongshu.com" or parsed.port not in (None,443)
                or parsed.username is not None or parsed.password is not None or parsed.path != "/404" or parsed.fragment):
            return False
        query = parse_qs(parsed.query, keep_blank_values=True)
    except ValueError:
        return False
    ids = [k for k in query if k.lower().replace("_","") == "noteid"]
    return ids == ["noteId"] and query["noteId"] == [note_id] and query.get("errorCode") == ["-510001"]


class NoteErrorPage:
    """Preserve the official note-specific error notice before its timed redirect to the home page."""
    def __init__(self, client, key, photos):
        self.client, self.key, self.photos = client, key, photos
        self.page = client._page
        self.requested = self.seen = self.reading = False
        self.image = self.session = None

    def listen(self, stack):
        source = getattr(self.client, "_audit_source_url", None)
        if not isinstance(source, str) or getattr(self.client, "_audit_note_id", None) != self.key:
            return
        parsed = urlparse(source)
        original = re.fullmatch(r"/(?:explore|discovery/item)/([a-zA-Z0-9]+)/?", parsed.path)
        try:
            allowed_origin = (parsed.scheme == "https" and parsed.hostname in ("www.xiaohongshu.com", "xiaohongshu.com")
                              and parsed.username is None and parsed.password is None and parsed.port in (None, 443))
        except ValueError:
            return
        if not allowed_origin or not original or original[1] != self.key:
            return
        if self.photos is not None:
            self.session = self.page.context.new_cdp_session(self.page)
            stack.callback(self.session.detach)
        def requested(request):
            if request.is_navigation_request() and request.url == source and request.frame == self.page.main_frame:
                self.requested = True
        def navigated(frame):
            if frame == self.page.main_frame:
                if frame.url == source:
                    self.requested = True
                elif official_note_error_url(frame.url, self.key):
                    self.inspect()
        for signal, callback in (("request", requested), ("framenavigated", navigated)):
            self.page.on(signal, callback)
            stack.callback(self.page.remove_listener, signal, callback)

    def inspect(self):
        if self.reading or not self.requested or not official_note_error_url(self.page.url, self.key) or self.image:
            return
        self.reading = True
        try:
            excluded = ".note-container,.comments-container,.comment-item,.note-content,.desc,[role=dialog],nav,header"
            notice = self.page.get_by_text(NOTE_ERROR_TEXT).and_(self.page.locator(
                f":visible:not(:is({excluded})):not(:is({excluded}) *)")).first
            notice.wait_for(state="visible", timeout=900)
            self.image = self.capture_notice(notice)
        except Exception:
            # The error component may already have navigated away.
            pass
        finally:
            self.reading = False

    def capture_notice(self, notice):
        # This page automatically navigates away. Capture the pixels of its currently
        # painted text directly, without locator scrolling, animation or font waits.
        read_box = """e => {
            const visibleText = document.body.innerText;
            if (/手机号登录|输入验证码|登录后推荐更懂你的笔记|登录后查看更多评论|请完成(?:安全)?验证|访问(?:过于)?频繁/.test(visibleText))
                return null;
            const gates = document.querySelectorAll('[role=dialog], .captcha-container, .verify-dialog, .security-dialog');
            if ([...gates].some(n => n.checkVisibility() && /安全验证|验证码|滑动验证/.test(n.innerText))) return null;
            const range = document.createRange();
            range.selectNodeContents(e);
            const boxes = [...range.getClientRects()].filter(r => r.width && r.height);
            if (!boxes.length) return null;
            for (let p = e; p; p = p.parentElement) {
                const css = getComputedStyle(p);
                if (css.visibility !== 'visible' || Number(css.opacity) < 0.95) return null;
            }
            for (const r of boxes) {
                if (r.left < 0 || r.top < 0 || r.right > innerWidth || r.bottom > innerHeight) return null;
                for (const part of [0.12, 0.5, 0.88]) {
                    const hit = document.elementFromPoint(r.left + part*r.width, r.top + r.height/2);
                    if (!hit || !(e.contains(hit) || (hit.contains(e) && getComputedStyle(e).pointerEvents === 'none')))
                        return null;
                }
            }
            const x = Math.max(0, Math.floor(Math.min(...boxes.map(r=>r.left)))-16);
            const y = Math.max(0, Math.floor(Math.min(...boxes.map(r=>r.top)))-12);
            const right = Math.min(innerWidth, Math.ceil(Math.max(...boxes.map(r=>r.right)))+16);
            const bottom = Math.min(innerHeight, Math.ceil(Math.max(...boxes.map(r=>r.bottom)))+12);
            return {text:e.innerText.trim(), url:location.href, timeOrigin:performance.timeOrigin,
                clip:{x:x+scrollX, y:y+scrollY, width:right-x, height:bottom-y, scale:1}};
        }"""
        for _ in range(6):
            before = notice.evaluate(read_box, timeout=250)
            if before and official_note_error_url(before["url"], self.key) and NOTE_ERROR_TEXT.fullmatch(before["text"]):
                self.seen = True
                if self.photos is None:
                    return None
                frame = self.session.send("Page.captureScreenshot", {"format":"png", "clip":before["clip"],
                                          "captureBeyondViewport":False, "fromSurface":True})
                if notice.evaluate(read_box, timeout=250) == before:
                    data = base64.b64decode(frame["data"], validate=True)
                    return self.photos.writer.save(notice, "unavailable", capture=lambda: data)
            if not official_note_error_url(self.page.url, self.key):
                break
            self.page.wait_for_timeout(70)

    def applies(self):
        self.inspect()
        if self.reading:
            deadline = time.monotonic() + 0.9
            while self.reading and time.monotonic() < deadline:
                self.page.wait_for_timeout(25)
        if not self.seen:
            return False
        current = str(self.page.url)
        if official_note_error_url(current, self.key):
            return True
        home = urlparse(current)
        return (home.scheme == "https" and home.netloc == "www.xiaohongshu.com"
                and home.path == "/explore" and not home.fragment
                and set(parse_qs(home.query, keep_blank_values=True)) <= {"source"})


def note_page_state(page, note_id):
    # xhs-cli falls back to the first map entry when the requested note is absent.
    # Inspect that exact entry before and after reading the displayed comments.
    return page.evaluate("""expected => {
        const map = window.__INITIAL_STATE__?.note?.noteDetailMap || {};
        const key = Object.hasOwn(map, expected) ? expected : Object.keys(map)[0];
        const note = map[key]?.note;
        const entity = note?.noteId || note?.note_id || note?.id;
        if (entity && String(entity) !== expected) return 'wrong_note';
        if (key && key !== expected && /^[a-f0-9]{24}$/i.test(key) && !entity) return 'wrong_note';
        const messages = document.querySelectorAll(
            '[role=alert], [class*="unavailable"], [class*="not-found"], [class*="error"], .access-wrapper');
        const restriction = /(?:这篇笔记已被删除|笔记不存在|笔记已删除|当前笔记暂时无法浏览|作者设置了权限|该笔记仅作者可见)/;
        for (const node of messages) {
            if (node.closest('.comment-item, .comments-container, .note-content, .desc')) continue;
            if (node.getClientRects().length && getComputedStyle(node).visibility === 'visible'
                    && restriction.test(node.innerText)) return 'post_unavailable';
        }
        if (entity || (key === expected && (note?.type || note?.interactInfo))) return 'target_note';
        return 'unknown';
    }""", note_id)


def collect_note_evidence(client, source_key, note_id, token, requirements, args, login_error, fetch_error, prior=None,
                          *, context_rows=None):
    directory = getattr(args, "screenshot_dir", None)
    photos = CommentPhotos(NoteScreenshots(directory, "xiaohongshu", source_key), client._page, note_id) if directory else None
    try:
        with ExitStack() as stack:
            error_page = NoteErrorPage(client, note_id, photos)
            error_page.listen(stack)
            return _collect_note_evidence(client, source_key, note_id, token, requirements, args, login_error, fetch_error,
                                          prior, context_rows=context_rows, photos=photos, error_page=error_page)
    except BaseException:
        if photos is not None:
            photos.writer.discard_all()
        raise


def _collect_note_evidence(client, source_key, note_id, token, requirements, args, login_error, fetch_error, prior,
                           *, context_rows, photos, error_page):
    def check_note_identity(check=None):
        active_note = target_from_url(str(client._page.url))
        page_state = note_page_state(client._page, note_id)
        if (not active_note or active_note["note_id"] != note_id or page_state == "wrong_note"
                or (detail_id and str(detail_id) != note_id)):
            error = fetch_error("note_identity_unconfirmed")
            if check:
                error.reply_verification = {**check, "status": "target_changed", "unconfirmed_after": None}
            raise error
        return page_state

    def restricted_result(official=False):
        observation = {"target_key": source_key, "platform": "xiaohongshu", "capture_status": "inaccessible",
                       "capture_complete": False, "image_detection_complete": False, "comments": [],
                       "target_page_state": "post_unavailable", "warnings": ["post_unavailable"]}
        if official:
            observation["availability_source"] = "official_note_error_page"
            if photos is not None:
                for shot in list(photos.writer.records):
                    if shot is not error_page.image:
                        photos.writer.discard(shot)
                photos.attach(observation, False)
        elif photos is not None:
            photos.writer.discard_all()
            messages = client._page.locator('[role=alert], [class*="unavailable"], [class*="not-found"], [class*="error"], .access-wrapper')
            reason = re.compile("这篇笔记已被删除|笔记不存在|笔记已删除|当前笔记暂时无法浏览|作者设置了权限|该笔记仅作者可见")
            visible = messages.filter(has_text=reason).and_(client._page.locator(":visible")).first
            if visible.count():
                photos.take(visible, "unavailable", valid=lambda: check_note_identity() == "post_unavailable"
                            and not reply_page_blocker(client))
            photos.attach(observation, False)
        return observation, False, False, declared

    declared = None
    detail_id = None
    structured_ok = True
    try:
        detail = client.get_note_detail(note_id, token)
        note = detail.get("note", detail) if isinstance(detail, dict) else {}
        if isinstance(note, dict):
            detail_id = note.get("noteId") or note.get("note_id") or note.get("id")
        declared = declared_comment_count(detail)
        raw = [] if (detail_id and str(detail_id) != note_id) or error_page.applies() else client.get_note_comments(note_id, token, max_comments=0)
    except login_error:
        raise
    except fetch_error as error:
        status = classify_failure(1, str(error))
        if status in ("session_expired", "captcha", "risk_control"):
            raise login_error(status if status != "session_expired" else "login required") from None
        if not requirements and not args.expand_dom and not error_page.applies():
            raise
        # An unavailable data view can still leave the ordinary note page usable.
        structured_ok = False
        raw = []
    blocker = reply_page_blocker(client)
    if blocker:
        raise login_error(blocker)
    if error_page.applies():
        return restricted_result(official=True)
    if error_page.image is not None:
        photos.writer.discard(error_page.image)
        error_page.image = None
    if check_note_identity() == "post_unavailable":
        return restricted_result()
    evidence, terminal, parse_failed = check_note_comments(
        client._page, source_key, raw, declared, requirements, args, structured_ok=structured_ok, prior=prior,
        context_rows=context_rows, resolved_note_id=note_id, photos=photos)
    check = evidence.get("reply_verification", {})
    blocker = reply_page_blocker(client)
    if blocker:
        error = login_error(blocker)
        if check.get("page_attempted"):
            error.reply_verification = {**check, "status": "blocked", "unconfirmed_after": None}
        raise error
    if error_page.applies():
        return restricted_result(official=True)
    page_state = check_note_identity(check)
    if page_state == "post_unavailable":
        return restricted_result()
    if evidence.get("capture_complete_source") == EMPTY_STATE_SOURCE and page_state != "target_note":
        raise fetch_error("note_identity_unconfirmed")
    if evidence["capture_status"] == "inaccessible":
        if page_state in SINGLE_NOTE_PAGE_STATES:
            evidence["target_page_state"] = page_state
        elif detail_id and str(detail_id) == note_id:
            evidence["target_page_state"] = "target_note"
    return evidence, terminal, parse_failed, declared


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True, help="Source XLSX; repeat for multiple files")
    parser.add_argument("--output", required=True)
    screenshot_options = parser.add_mutually_exclusive_group()
    screenshot_options.add_argument("--screenshot-dir", type=Path, help="Directory for original comment screenshots (default: 评论截图 beside --output)")
    screenshot_options.add_argument("--no-screenshots", action="store_true", help="Capture comment observations without PNG files")
    parser.add_argument("--expected", "--verify-replies", action="append", default=[], metavar="EXPECTED_JSON",
                        help="Source task rows to check against comments anywhere on the same note")
    parser.add_argument("--require-reply-parent", action="store_true",
                        help="Also collect direct-parent evidence for explicit thread-attribution diagnostics")
    parser.add_argument("--task-targets", action="store_true",
                        help="Use only source rows in --expected and their mapped original note links")
    parser.add_argument("--map", action="append", default=[], help="Source column mapping for --task-targets")
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
    parser.add_argument("--skip-note-id", action="append", default=[], help="Skip a note already probed in this pass")
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

    mapping = parse_maps(args.map)
    groups = capture_task_groups(args.input, args.expected, mapping)
    task_rows = [row for group in groups for row in group]
    if (args.task_targets or args.require_reply_parent) and not task_rows:
        parser.error("task targets and reply-parent diagnostics need task rows in --expected")
    scope = recheck_scope(task_rows)
    contexts = group_note_requirements(task_rows) if scope else {}
    if scope:
        distinct_paths = {Path(path).resolve() for path in args.input}
        if len({path.name for path in distinct_paths}) < len(distinct_paths):
            parser.error("success-only capture requires distinct input workbook names")
        task_rows = [row for group in groups for row in rows_to_review(group)]
    scoped_files = {row.get("source_file") for group in groups for row in group}
    unfiltered = {path for path in args.input if not args.task_targets and Path(path).name not in scoped_files}
    targets = targets_from_workbooks(args.input, task_rows=task_rows if args.task_targets or scope else None,
                                     mapping=mapping, unfiltered_paths=unfiltered)
    if args.only_note_id:
        targets = [target for target in targets if target["note_id"] in set(args.only_note_id)]
        if not targets and not scope:
            parser.error("--only-note-id was not found in the input workbooks")
    if args.skip_note_id:
        skipped = set(args.skip_note_id)
        targets = [target for target in targets if target["note_id"] not in skipped]
    if args.limit is not None:
        targets = targets[: max(0, args.limit)]
    output_path = Path(args.output)
    args.screenshot_dir = None if args.no_screenshots else (args.screenshot_dir or output_path.parent / "评论截图").resolve()
    if any(output_path.resolve() == Path(path).resolve() or (output_path.exists() and Path(path).exists() and output_path.samefile(path))
           for path in (*args.expected, *args.input)):
        parser.error("--output must differ from --expected and --input")
    checkpoint = json.loads(output_path.read_text(encoding="utf-8")) if output_path.exists() else {}
    prior_scope = checkpoint.get("recheck_scope") if isinstance(checkpoint, dict) else None
    if output_path.exists() and prior_scope != scope:
        parser.error("capture_scope_changed: use a new --output path for a success-only recheck")
    original = unwrap_rows(checkpoint, "observations") if checkpoint else []
    scope_meta = {"recheck_scope": scope} if scope else {}
    previous = {target_key(item): item for item in original if platform_key(item) == "xiaohongshu"}
    existing = previous.copy()

    total_targets = len(targets)
    requirements = group_note_requirements(task_rows)
    if args.require_reply_parent:
        requirements = {key: rows for key, rows in requirements.items() if any(row.get("reply_to") for row in rows)}
    targets = plan_targets(targets, existing, requirements, args.refresh, require_reply_parent=args.require_reply_parent,
                           contexts=contexts, screenshot_dir=args.screenshot_dir)
    print(f"[resume-plan] total={total_targets} reused={total_targets-len(targets)} pending={len(targets)}", flush=True)
    if not targets:
        if scope and not output_path.exists():
            atomic_write(output_path, {**scope_meta, "observations": [], "stopped": False})
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
            atomic_write(output_path, {**scope_meta, "observations": saved_observations(original, previous, existing), "stopped": True,
                                       "stop_reason": stop_reason, "stop_phase": "startup"})
            print(f"[startup] stop status={stop_reason}", flush=True)
            confirmed = args.wait_for_confirmation and hold_verification(client)
            return 4 if confirmed else 3
        if args.session_settle_max:
            settle = random.uniform(args.session_settle_min, args.session_settle_max)
            print(f"[session-settle] seconds={settle:.1f} profile={'risk-averse' if args.risk_averse else 'standard'}", flush=True)
            paced_sleep(settle)
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
                atomic_write(output_path, {**scope_meta, "observations": saved_observations(original, previous, existing), "stopped": True, "stop_reason": stop_reason})
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
                    None if args.refresh else existing.get(note_id), context_rows=contexts.get(note_id))
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
                    observation.setdefault("warnings", []).append(
                        "comment_dom_extraction_incomplete" if dom_parse_failed else "comments_pagination_incomplete")
                elif dom_terminal and declared is not None and len(captured) < declared:
                    observation["warnings"] = ["visible_terminal_with_stale_declared_count"]
                existing[note_id] = observation
                processed_count += 1
                arrived = evidence["capture_status"] == "ok" or evidence.get("target_page_state") in SINGLE_NOTE_PAGE_STATES
                consecutive_errors = 0 if arrived else consecutive_errors + 1
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
                    "captured_at": captured_at, "warnings": [
                        "note_identity_unconfirmed" if str(exc) == "note_identity_unconfirmed" else "note_data_unavailable"],
                    "comments": [], "source_refs": target["sources"],
                }
                if hasattr(exc, "reply_verification"):
                    existing[note_id]["reply_verification"] = exc.reply_verification
                print(f"[{index}/{len(targets)}] inaccessible {note_id}", flush=True)
                processed_count += 1
                consecutive_errors += 1
                if consecutive_errors >= 2:
                    stop_reason = "repeated_navigation_failure"
            if args.screenshot_dir is not None and "screenshot_status" not in existing[note_id]:
                existing[note_id].update(screenshot_status="missing", comment_screenshots=[])
            existing[note_id]["capture_duration_ms"] = round((time.monotonic() - target_started) * 1000)
            atomic_write(output_path, {**scope_meta, "observations": saved_observations(original, previous, existing), "stopped": bool(stop_reason), "stop_reason": stop_reason})
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
                paced_sleep(pause)
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
            paced_sleep(pause)
    finally:
        client.close()
    return 3 if stop_reason else 0


if __name__ == "__main__":
    configure_stdio()
    raise SystemExit(main())
