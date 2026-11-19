"""Network-free Chrome cases for asynchronous replies and panel rendering."""
from argparse import Namespace


def deferred_capture(page, collector, mode):
    page.goto("about:blank")
    root = '<div class="comment-item" data-comment-id="root"><div class="content">root</div></div>'
    child = '<div class="comment-item" data-comment-id="child"><div class="content">visible reply</div></div>'
    end = '<span>没有更多评论</span>'
    panel = '<div class="comments-container"><div class="parent-comment">' + root
    args = Namespace(expand_dom=True, dom_max_rounds=2, incomplete_dom_retry_rounds=1)
    if mode == "panel":
        page.set_content('<template id="ready">' + panel + child + '</div>' + end + '</div></template>'
                         '<script>setTimeout(() => document.body.append(ready.content.cloneNode(true)), 350)</script>')
    elif mode in {"loading", "stalled"}:
        finish = ('<script>setTimeout(() => {loading.remove(); replies.append(ready.content.cloneNode(true))}, 400)</script>'
                  if mode == "loading" else "")
        page.set_content(panel + '</div><span id="loading" class="comments-loading" role="progressbar">加载中</span>'
                         '<div id="replies"></div>' + end + '</div><template id="ready">' + child + '</template>' + finish)
    else:
        delay = 2700 if mode == "late" else 0
        needed = 2 if mode in {"partial", "staged", "duplicate"} else 1
        dest = "unrelated" if mode == "unrelated" else "children"
        insert = (f"setTimeout(() => document.getElementById('{dest}').append("
                  f"document.getElementById('child').content.cloneNode(true)), {delay})")
        if mode == "empty":
            insert = ""
        elif mode in {"staged", "duplicate"}:
            identifier = "second" if mode == "staged" else "child"
            insert += (";setTimeout(() => {const fragment=document.getElementById('child').content.cloneNode(true);"
                       f"fragment.firstElementChild.dataset.commentId='{identifier}';"
                       "document.getElementById('children').append(fragment)}, 400)")
        page.set_content(panel + '<div id="children"></div>'
                         f'<button onclick="this.remove();{insert}">展开{needed}条回复</button></div>'
                         '<div id="unrelated"></div>' + end + '</div><template id="child">' + child + '</template>')
    observation, _, _ = collector.check_note_comments(page, "note", [], None, [], args)
    return observation


def exercise_deferred_comments(page, collector):
    for mode in ("panel", "loading", "stalled", "late", "fast", "staged", "duplicate", "partial", "empty", "unrelated"):
        observed = deferred_capture(page, collector, mode)
        expect_child = mode not in {"empty", "stalled"}
        assert any(c["text"] == "visible reply" for c in observed["comments"]) == expect_child, mode
        assert observed["capture_complete"] == (mode in {"panel", "loading", "late", "fast", "staged"}), mode
        if mode in {"duplicate", "partial", "empty", "unrelated"}:
            assert "reply_expansion_unconfirmed" in observed.get("warnings", []), mode
