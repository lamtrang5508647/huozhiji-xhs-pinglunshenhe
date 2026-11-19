#!/usr/bin/env python3
"""Offline regression tests for XHS DOM expansion parsing and completeness."""

from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from xhs_batch_capture import apply_risk_averse_profile, parse_dom_comment
from xhs_batch_capture import target_from_url, targets_from_workbooks, visible_terminal, expand_dom_comments, prefer_comment_snapshot
from xhs_batch_capture import check_note_comments, collect_note_evidence, plan_targets, reply_signature, unconfirmed_replies
from capture_resume import PARSER_REVISIONS
from review_comments import compare


class FakeLocator:
    def __init__(self, count: int):
        self._count = count

    def count(self) -> int:
        return self._count

    def and_(self, other):
        return self


class FakeItem:
    def evaluate(self, expression):
        body = self.locator(".content .note-text")
        own_text = body.count() == 1
        return {"text": body.first.inner_text() if own_text else self.inner_text(), "own_text": own_text,
                "has_image": self.locator(".comment-picture img").count() > 0}
    def inner_text(self, timeout: int = 0) -> str:
        return "昵称\n隐藏回复正文\n昨天 16:34广东\n赞\n回复"

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(1 if selector == ".comment-picture img" else 0)


class FakeTextLocator(FakeLocator):
    def __init__(self, text):
        super().__init__(1)
        self.text = text
        self.first = self

    def inner_text(self, timeout=0):
        return self.text


class FakeReplyItem(FakeItem):
    def __init__(self, body):
        self.body = body

    def inner_text(self, timeout=0):
        return f"昵称\n回复 示例用户 : {self.body}\n昨天 16:34广东\n赞\n回复"

    def locator(self, selector):
        if selector == ".content .note-text":
            return FakeTextLocator(self.body)
        return FakeLocator(0)


def test_target_column_and_carried_rows():
    sheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    document_rels = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_rels = "http://schemas.openxmlformats.org/package/2006/relationships"
    with TemporaryDirectory() as directory:
        path = Path(directory) / "capture.xlsx"
        with ZipFile(path, "w") as archive:
            archive.writestr("xl/workbook.xml", f'''<workbook xmlns="{sheet_ns}" xmlns:r="{document_rels}">
                <sheets><sheet name="Tasks" sheetId="1" r:id="rId1"/></sheets></workbook>''')
            archive.writestr("xl/_rels/workbook.xml.rels", f'''<Relationships xmlns="{package_rels}">
                <Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="{document_rels}/worksheet"/>
                </Relationships>''')
            archive.writestr("xl/worksheets/sheet1.xml", f'''<worksheet xmlns="{sheet_ns}"><sheetData>
                <row r="2"><c r="AA2" t="inlineStr"><is><t>https://www.xiaohongshu.com/explore/wrong</t></is></c></row>
                <row r="3"><c r="A3" t="inlineStr"><is><t>https://www.xiaohongshu.com/explore/right</t></is></c></row>
                <row r="4"><c r="AB4" t="inlineStr"><is><t>https://www.xiaohongshu.com/explore/other</t></is></c></row>
                </sheetData></worksheet>''')
        targets = targets_from_workbooks([str(path), str(path)])
        assert [target["note_id"] for target in targets] == ["right"]
        assert targets[0]["sources"] == [
            {"file": path.name, "sheet": "Tasks", "row": 3},
            {"file": path.name, "sheet": "Tasks", "row": 4},
        ]


def reply_case():
    base = {"platform": "xiaohongshu", "target_key": "note", "comment_type": "text", "expected_amount": "1.00"}
    rows = [{**base, "case_id": "a", "comment_text": "First", "source_row": 2},
            {**base, "case_id": "b", "comment_text": "Reply", "source_row": 3, "reply_to": "回复第一条"}]
    comments = [{"id": "c1", "text": "First", "subComments": [{"id": "c2", "text": "Reply"}]}]
    return rows, comments


def exercise_real_reply_recheck(page):
    # The initial snapshot has the correct count and text, but no immediate parent.
    rows, comments = reply_case()
    page.set_content("""
      <button onclick="document.querySelector('.comments-container').hidden=false">查看全部评论</button>
      <div class="comments-container" hidden><div class="comment-item" data-comment-id="c1">
        <div class="content"><div class="note-text">First</div></div>
        <button onclick="document.getElementById('children').innerHTML=document.getElementById('child').innerHTML;
                         this.remove()">展开1条回复</button>
        <div data-direct-replies id="children"></div>
      </div><span>没有更多评论</span></div>
      <template id="child"><div class="comment-item" data-comment-id="c2">
        <div class="content"><span class="note-text">Reply</span></div></div></template>""")
    args = Namespace(expand_dom=True, dom_max_rounds=1, incomplete_dom_retry_rounds=0)
    evidence, terminal, failed = check_note_comments(page, "note", comments, 2, rows, args)
    check = evidence["reply_verification"]
    assert terminal and not failed and check["page_attempted"] and check["panel_available"]
    assert (check["unconfirmed_before"], check["unconfirmed_after"]) == (1, 0)
    assert compare(rows, [evidence], amount_mode="expected-only")["results"][1]["triage"] == "成功"
    # A generic flattened conversation container cannot prove the direct recipient.
    page.locator("#children").evaluate("node => node.removeAttribute('data-direct-replies')")
    flat, _, _ = expand_dom_comments(page, max_rounds=0)
    assert "parent_comment_id" not in flat[1] and "parent_text" not in flat[1]

    # A normal data-view failure still permits an ordinary visible page to supply evidence.
    class FetchError(Exception):
        pass
    class LoginError(Exception):
        pass
    class LocalPage:
        url = "https://www.xiaohongshu.com/explore/note"
        def __getattr__(self, name):
            return getattr(page, name)
    class Client:
        _page = LocalPage()
        def get_note_detail(self, note, token):
            raise FetchError("ordinary data unavailable")
    page.locator("#children").evaluate("node => node.setAttribute('data-direct-replies', '')")
    args.dom_max_rounds = 0
    evidence, _, _, _ = collect_note_evidence(Client(), "note", "note", "", rows, args, LoginError, FetchError)
    assert evidence["reply_verification"]["structured_read"] == "unavailable"
    assert evidence["reply_verification"]["status"] == "checked"
    assert compare(rows, [evidence], amount_mode="expected-only")["results"][1]["triage"] == "成功"
    empty, _, _ = check_note_comments(page, "note", [], 0, rows, args)
    assert empty["reply_verification"]["status"] == "checked" and len(empty["comments"]) == 2
    page.set_content('<main>Note without available comments panel</main>')
    blocked, _, _, _ = collect_note_evidence(Client(), "note", "note", "", rows, args, LoginError, FetchError)
    assert blocked["capture_status"] == "inaccessible" and not blocked["capture_complete"]
    assert blocked["reply_verification"]["status"] == "panel_unavailable"

    # A deleted thread root and a matching question in another thread are independent facts.
    page.set_content("""
      <div class="comments-container">
        <div class="parent-comment">
          <div class="comment-item" data-comment-id="gone"><div class="content"><span class="note-text">原评论已删除</span></div></div>
          <div class="reply-container"><div class="list-container">
            <div class="comment-item comment-item-sub" data-comment-id="c2">
              <div class="content"><span class="note-text">Reply</span></div></div>
          </div></div></div>
        <div class="parent-comment"><div class="comment-item" data-comment-id="c1">
          <div class="content"><span class="note-text">First</span></div></div></div>
        <span>没有更多评论</span></div>""")
    evidence, _, _ = check_note_comments(page, "note", comments, 2, rows, args)
    root, reply, other = evidence["comments"]
    assert root.get("deleted") and not root["text"]
    assert reply["thread_root_deleted"] and "parent_comment_id" not in reply and "parent_text" not in reply
    assert "thread_root_deleted" not in other
    assert evidence["reply_verification"]["status"] == "checked_unresolved"
    assert compare(rows, [evidence], amount_mode="expected-only")["results"][1]["triage"] == "待人审"


def exercise_visible_comments(page):
    args = Namespace(expand_dom=True, dom_max_rounds=0, incomplete_dom_retry_rounds=0)
    def observe(body, raw=(), declared=None):
        page.set_content('<div class="comments-container">' + body + '</div>')
        return check_note_comments(page, "note", list(raw), declared, None, args)[0]
    def item(text, identifier="v"):
        return f'<div class="comment-item" data-comment-id="{identifier}"><div class="content">{text}</div></div>'
    end = '<span>没有更多评论</span>'
    assert observe(item("shown") + end)["comments"][0]["text"] == "shown"
    assert observe(item('A<img class="emoji" alt="[笑]">B') + end)["comments"][0]["text"] == "A[笑]B"
    assert not observe(item("没有更多评论"), declared=1)["capture_complete"]
    assert not observe(item("shown") + end + '<button>展开2条回复</button>', declared=1)["capture_complete"]
    assert not observe(item("shown"), [{"id":"v","text":"shown"}], 1)["capture_complete"]
    assert not observe("<span>正在加载</span>", declared=0)["capture_complete"]
    assert observe('<div class="empty">暂无评论</div>', declared=0)["capture_complete"]
    assert not observe(item("shown") + '<div class="empty">暂无评论</div>')["capture_complete"]
    assert not observe('<div class="comment-item">unparsed</div>' + end)["capture_complete"]
    assert not observe(item("shown") + end, [{"id":"v","text":"shown","subCommentCount":3}], 1)["capture_complete"]
    assert observe(item("shown") + '<div class="comment-item" hidden><div class="content">hidden</div></div>' + end)["comments"][0]["text"] == "shown"
    stale = [{"id":"old","text":"removed"}, {"id":"v","text":"old content"}]
    assert [c["text"] for c in observe(item("now shown") + end, stale)["comments"]] == ["now shown"]
    partial = observe(item("now shown"), stale)
    assert {c["text"] for c in partial["comments"]} == {"removed", "now shown"} and not partial["capture_complete"]
    args.dom_max_rounds, args.incomplete_dom_retry_rounds = 1, 2
    recycle = """<button onclick="document.getElementById('window').innerHTML=document.getElementById('last').innerHTML;
                  this.remove()">查看更多评论</button>"""
    window = ('<div id="window">' + item("first visible", "first") + '</div>' + recycle
              + '<template id="last">' + item("last visible", "last") + end + '</template>')
    recycled = observe(window)
    assert {c["text"] for c in recycled["comments"]} == {"first visible", "last visible"}
    assert not recycled["capture_complete"]

    rows, raw = reply_case()
    def thread(root="First", reply="Reply", extra="", prefix=""):
        return (f'<div class="parent-comment">{item(root, "c1").replace(root, "<span class=note-text>" + root + "</span>")}'
                '<div class="reply-container"><div class="list-container">'
                '<div class="comment-item comment-item-sub" data-comment-id="c2">'
                f'<div class="content">{prefix}<span class="note-text">{reply}</span></div></div>'
                f'{extra}</div></div></div>')
    args.dom_max_rounds = args.incomplete_dom_retry_rounds = 0
    safe = observe(thread() + end, raw)
    reply = safe["comments"][1]
    assert reply["parent_text"] == "First" and reply["parent_evidence"] == "single_visible_reply_under_root"
    assert "parent_comment_id" not in reply
    assert compare(rows, [safe], amount_mode="expected-only")["results"][1]["triage"] == "成功"
    elsewhere = [{"id":"other", "text":"Unrelated", "subComments":[{"id":"c2", "text":"Reply"}]}]
    directed = [{"id":"c1", "text":"First", "subComments":[{"id":"c2","text":"Reply","targetCommentId":"other"}]}]
    other_child = [{"id":"c1", "text":"First", "subComments":[{"id":"another","text":"Another"}]}]
    for body, structured in [(thread(), []), (thread(extra=item("third","c3")) + end, []),
            (thread(prefix="回复 小明：") + end, []), (thread(root="原评论已删除") + end, []),
            (thread() + end + '<button>展开1条回复</button>', []), (thread() + end, elsewhere),
            (thread() + end, directed), (thread() + end, other_child),
            (thread(reply="回复 @某人 Reply") + end, []), (thread() + end, [{"id":"c2","text":"Reply"}])]:
        evidence = observe(body, structured)
        assert "parent_evidence" not in evidence["comments"][1]
        assert compare(rows, [evidence], amount_mode="expected-only")["results"][1]["triage"] == "待人审"


def test_reply_resume():
    from datetime import datetime, timezone
    from xhs_capture import normalized_comments
    rows, comments = reply_case()
    key = "note"
    fresh = {"platform": "xiaohongshu", "target_key": key, "capture_status": "ok", "capture_complete": True,
             "parser_revision": PARSER_REVISIONS["xiaohongshu"], "captured_at": datetime.now(timezone.utc).isoformat(),
             "comments": normalized_comments(comments, root_list=True)}
    targets = [{"note_id": key}]
    requirements = {key: rows}
    assert unconfirmed_replies(rows, fresh) == 1
    assert plan_targets(targets, {key: fresh}, requirements, False) == targets
    check = {"page_attempted": True, "panel_available": True, "requirements_sha256": reply_signature(rows)}
    assert not plan_targets(targets, {key: {**fresh, "reply_verification": check}}, requirements, False)
    assert plan_targets(targets, {key: {**fresh, "reply_verification": {**check, "panel_available": False}}},
                        requirements, False) == targets
    capped = {**fresh, "reply_verification": {**check, "panel_available": False, "attempt_count": 2}}
    assert not plan_targets(targets, {key: capped}, requirements)
    assert plan_targets(targets, {key: capped}, requirements, refresh=True) == targets
    altered = {key: [*rows[:1], {**rows[1], "comment_text": "Other"}]}
    assert plan_targets(targets, {key: {**fresh, "reply_verification": check}}, altered, False) == targets
    comments[0]["subComments"][0]["target_comment_id"] = "c1"
    args = Namespace(expand_dom=False, dom_max_rounds=1, incomplete_dom_retry_rounds=0)
    satisfied, _, _ = check_note_comments(None, key, comments, 2, rows, args)
    assert satisfied["reply_verification"]["page_attempted"] is False
    assert not satisfied["capture_complete"]
    assert not plan_targets(targets, {key: {**fresh, "comments":satisfied["comments"]}}, requirements, False)


def main() -> None:
    test_reply_resume()
    test_target_column_and_carried_rows()
    assert parse_dom_comment(FakeReplyItem('请问喝哪家啊？'))['text'] == '请问喝哪家啊？'
    assert parse_dom_comment(FakeReplyItem('回复 小明：这是作者自己写的正文'))['text'] == '回复 小明：这是作者自己写的正文'
    long_target = target_from_url('标题 https://www.xiaohongshu.com/explore/n1?xsec_token=fixture&xsec_source=pc_ad')
    assert long_target['note_id'] == 'n1' and long_target['token'] == 'fixture'
    assert long_target['source_url'].endswith('/n1?xsec_token=fixture&xsec_source=pc_ad')
    short_target = target_from_url('https://xhslink.cn/o/example')
    assert short_target['note_id'] == 'example' and short_target['short_url'] == 'https://xhslink.cn/o/example'
    assert target_from_url('https://v.douyin.com/example/') is None
    assert target_from_url('https://example.org/?url=xiaohongshu.com/explore/n1') is None
    assert parse_dom_comment(FakeItem()) == {"text": "隐藏回复正文", "has_image": True}
    class ImageOnly(FakeItem):
        def inner_text(self, timeout=0):
            return "昵称"
    assert parse_dom_comment(ImageOnly()) == {"text": "", "has_image": True}
    class Terminal:
        def __init__(self, visible):
            self.visible = visible
        def get_by_text(self, *args, **kw):
            return FakeLocator(int(self.visible))
        def locator(self, selector):
            return FakeLocator(0)
    assert not visible_terminal(Terminal(False))
    assert visible_terminal(Terminal(True))
    from types import SimpleNamespace
    class Items:
        def __init__(self, values):
            self.values = values
            self.last = SimpleNamespace(scroll_into_view_if_needed=lambda **kw: None)
        def count(self):
            return len(self.values)
        def evaluate_all(self, expression):
            self.calls = getattr(self, "calls", 0) + 1
            rows = []
            for value in self.values:
                try:
                    rows.append(value.evaluate(expression))
                except Exception:
                    rows.append(None)
            return rows
    class Container(Terminal):
        first = property(lambda self: self)
        def is_visible(self):
            return True
        def count(self):
            return 1
        def locator(self, selector):
            return self.items
        def get_by_text(self, marker, **kw):
            return FakeLocator(1 if marker.search("没有更多评论") else 0)
    container = Container(True)
    container.items = Items([ImageOnly()])
    page = SimpleNamespace(locator=lambda selector: container, wait_for_timeout=lambda ms: None,
                           mouse=SimpleNamespace(wheel=lambda *args: None))
    comments, terminal, parsed = expand_dom_comments(page, max_rounds=1)
    assert comments == [{"text":"", "has_image":True}] and terminal and parsed
    class Broken(ImageOnly):
        def locator(self, selector):
            raise RuntimeError("synthetic DOM parsing failure")
    container.items = Items([ImageOnly(), Broken()])
    comments, terminal, parsed = expand_dom_comments(page, max_rounds=1)
    assert len(comments) == 1 and terminal and not parsed
    assert container.items.calls == 1
    structured = [{"text": "reply", "has_image": False, "comment_id": "c1", "parent_comment_id": "c0"}]
    anonymous = {"text": "reply", "has_image": False}
    identified = {**anonymous, "comment_id": "c1"}
    assert prefer_comment_snapshot(structured, [anonymous]) == structured
    assert prefer_comment_snapshot(structured, [anonymous, anonymous]) == [anonymous, anonymous]
    assert prefer_comment_snapshot(structured, [identified, anonymous]) == [structured[0], anonymous]
    assert prefer_comment_snapshot(structured, [identified, identified]) == [identified, identified]
    conflict = {**identified, "parent_comment_id": "other"}
    assert prefer_comment_snapshot(structured, [conflict, anonymous]) == [identified, anonymous]
    args = Namespace(
        delay_min=6.0, delay_max=10.0, minimum_rest=1.5,
        break_every=12, break_min=25.0, break_max=45.0,
        dom_max_rounds=30, incomplete_dom_retry_rounds=0,
        session_settle_min=0.0, session_settle_max=0.0,
        warmup_targets=0, warmup_min=0.0, warmup_max=0.0,
    )
    apply_risk_averse_profile(args)
    assert (args.delay_min, args.delay_max, args.minimum_rest) == (12.0, 18.0, 4.0)
    assert (args.break_every, args.break_min, args.break_max) == (8, 60.0, 90.0)
    assert (args.dom_max_rounds, args.incomplete_dom_retry_rounds) == (45, 20)
    assert (args.session_settle_min, args.session_settle_max) == (8.0, 15.0)
    assert (args.warmup_targets, args.warmup_min, args.warmup_max) == (3, 20.0, 35.0)
    print("xhs batch capture tests passed")


if __name__ == "__main__":
    main()
