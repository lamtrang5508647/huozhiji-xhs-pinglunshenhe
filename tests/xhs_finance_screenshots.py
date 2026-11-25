"""Read actual local Chrome PNGs for folded replies, overflow panels and verified empty states."""
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from playwright.sync_api import Locator


def exercise_finance_screenshots(page, xhs):
    class LocalPage:
        url = "https://www.xiaohongshu.com/explore/note"
        def __getattr__(self, name):
            return getattr(page, name)

    actual = LocalPage()
    client = SimpleNamespace(_page=actual, get_note_detail=lambda *_: {"note":{"noteId":"note"}},
                             get_note_comments=lambda *a, **k: [])
    def show(markup):
        page.set_content(markup)
        page.evaluate("window.__INITIAL_STATE__={note:{noteDetailMap:{note:{note:{noteId:'note'}}}}}")
    def capture(folder, rows=None):
        args = SimpleNamespace(screenshot_dir=folder, expand_dom=True, dom_max_rounds=2, incomplete_dom_retry_rounds=0)
        observation, *_ = xhs.collect_note_evidence(client, "note", "note", "", rows or [], args, RuntimeError, ValueError)
        return observation
    def card(cid, body):
        return f'<div class="comment-item" data-comment-id="{cid}"><div class="content"><span class="note-text">{body}</span></div></div>'
    with TemporaryDirectory(prefix="xhs-finance-") as tmp:
        folder = Path(tmp) / "财务截图"
        show('<section class="comments-container"><div class="parent-comment">' + card("a","主评论") +
             '<button onclick="this.remove();setTimeout(()=>document.querySelector(\'.reply-container\').append('
             'document.querySelector(\'template\').content.cloneNode(true)),90)">展开1条回复</button>'
             '<div class="reply-container"></div></div><span>没有更多评论</span></section><template>' + card("b","真实回复") + '</template>')
        result = capture(folder)
        assert result["screenshot_status"] == "captured" and result["capture_complete"], result
        last = next(r for r in result["comment_screenshots"] if r["kind"]=="end")
        assert set(last["comment_ids"]) == {"a","b"} and set(last["comment_texts"]) == {"主评论","真实回复"}, last
        file = Path(tmp) / last["path"]
        assert file.read_bytes() == page.locator("section").screenshot(type="png", animations="disabled")
        assert next(c for c in result["comments"] if c["comment_id"]=="b")["evidence_path"] == last["path"]
        result.update(captured_at=datetime.now(timezone.utc).isoformat(), parser_revision=xhs.PARSER_REVISIONS["xiaohongshu"])
        assert xhs.capture_covers_tasks(result,"note",[],screenshot_dir=folder)
        file.unlink()
        assert not xhs.capture_covers_tasks(result,"note",[],screenshot_dir=folder)
        show('<style>.comment-item{height:85px}main{height:140px;overflow-y:auto}</style><main>'
             '<section class="comments-container">' + "".join(card(str(i),f"第{i}条") for i in range(7)) +
             '<span>没有更多评论</span></section></main>')
        tall = capture(folder)
        assert tall["screenshot_status"] == "captured" and tall["capture_complete"], tall
        assert len({c["evidence_path"] for c in tall["comments"]}) == 7
        assert {r["kind"] for r in tall["comment_screenshots"]} == {"comment","end"}
        show('<style>.comments-container{width:400px}.comment-item{height:90px}'
             '.float{position:absolute;left:170px;top:32px;width:80px;height:52px;background:#444}</style>'
             '<div class="comments-container">' + card("overlay","文字在浮条旁边") +
             '<span>没有更多评论</span></div><div class="float"></div>')
        narrow = capture(folder)
        cropped = narrow["comments"][0]["evidence_path"]
        assert (Path(tmp)/cropped).read_bytes() == page.locator(".note-text").screenshot(type="png",animations="disabled")
        show('<div class="note-container"><div class="interaction-container"><div class="note-scroller"><div class="comments-el">'
             '<div class="no-comments" style="pointer-events:none"><span class="no-comments-image" style="display:block;height:2px"></span>'
             '<span class="no-comments-text">这是一片荒地 点击评论</span></div></div></div></div></div>')
        # The live page's separate empty component was visible and stationary while
        # Playwright's extra scroll-action wait continued to time out.
        with patch.object(Locator, "scroll_into_view_if_needed", side_effect=TimeoutError("scroll action not settled")):
            empty = capture(folder)
        assert empty["capture_complete_source"] == "visible_comment_empty_state" and empty["screenshot_status"] == "captured"
        proof = empty["comment_screenshots"]
        assert len(proof) == 1 and proof[0]["kind"] == "empty"
        assert (Path(tmp)/proof[0]["path"]).read_bytes() == page.locator(".no-comments").screenshot(type="png", animations="disabled")
        page.locator(".no-comments").evaluate("""e=>{
            e.style.cssText = 'width:400px;height:140px'; e.lastElementChild.style.cssText='display:inline-block;margin-top:100px';
            const overlay = document.createElement('div'); overlay.style.cssText='position:absolute;top:42px;left:175px;width:64px;height:60px;background:#ddd';
            document.body.append(overlay);
        }""")
        label_only = capture(folder)["comment_screenshots"]
        assert len(label_only) == 1 and label_only[0]["kind"] == "empty"
        assert (Path(tmp)/label_only[0]["path"]).read_bytes() == page.locator(".no-comments-text").screenshot(type="png",animations="disabled")
        show('<div class="comments-container"><div class="comment-item" data-comment-id="p">'
             '<div class="content"><span class="note-text">附件</span></div><div class="comment-picture"><img></div>'
             '</div><span>没有更多评论</span></div>')
        broken = capture(folder)
        assert broken["comments"][0]["has_image"] and not broken["comments"][0].get("evidence_path")
        assert broken["screenshot_status"] == "partial", broken
        page.locator("img").evaluate("""e=>setTimeout(()=>{e.src='data:image/svg+xml,'+
            encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="60" height="30"><rect width="60" height="30" fill="green"/></svg>')},100)""")
        loaded = capture(folder)
        assert loaded["screenshot_status"] == "captured" and loaded["comments"][0]["evidence_path"]
        show('<div class="error">这篇笔记已被删除</div>')
        restricted = capture(folder)
        assert restricted["capture_status"]=="inaccessible" and restricted["comment_screenshots"][0]["kind"]=="unavailable"
        show('<section class="comments-container">' + card("r","不要串图") + '<span>没有更多评论</span></section>')
        previous = set(folder.rglob("*.png"))
        original = xhs.NoteScreenshots.save
        def navigate(writer, *args, **kwargs):
            shot = original(writer, *args, **kwargs)
            page.evaluate("window.__INITIAL_STATE__.note.noteDetailMap.note.note.noteId='other'")
            return shot
        with patch.object(xhs.NoteScreenshots,"save",navigate):
            try:
                capture(folder)
            except ValueError as exc:
                assert str(exc) == "note_identity_unconfirmed", exc
            else:
                raise AssertionError("Changed note supplied foreign screenshot")
        assert set(folder.rglob("*.png")) == previous
    page.goto("about:blank")
