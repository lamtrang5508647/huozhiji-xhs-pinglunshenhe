"""Synthetic page for the separately rendered, empty Xiaohongshu comment area."""
from argparse import Namespace


def exercise_empty_state(page, collector):
    class LocalPage:
        url = "https://www.xiaohongshu.com/explore/sample"
        def __getattr__(self, name):
            return getattr(page, name)

    actual = LocalPage()
    label = '<span class="no-comments-text"><span>这是一片荒地</span> <span>点击评论</span></span>'
    empty = '<div class="no-comments"><span class="no-comments-image"></span>' + label + '</div>'
    root = '<div class="note-container"><div class="interaction-container"><div class="note-scroller">'
    end = '</div></div></div>'
    region = '<div class="comments-el">' + empty + '</div>'
    ordinary = ('<div class="comments-container"><div class="comment-item" data-comment-id="reply">'
                '<div class="content"><span class="note-text">wanted</span></div></div><span>没有更多评论</span></div>')
    def show(html, entity="sample"):
        page.goto("about:blank")
        page.set_content('<style>.no-comments-image {display:block;width:4px;height:4px}</style>' + html)
        page.evaluate("id => window.__INITIAL_STATE__ = {note:{noteDetailMap:{sample:{note:{noteId:id}}}}}", entity)
    args = Namespace(expand_dom=True, dom_max_rounds=1, incomplete_dom_retry_rounds=0)
    def capture(declared):
        return collector.check_note_comments(actual, "sample", [], declared, [], args, structured_ok=False)

    show(root + '<div style="height:2400px"></div>' + region + end)
    result, terminal, failed = capture(2)
    assert terminal and not failed and result["capture_complete"], result
    assert page.evaluate("window.scrollY") > 0
    assert result["comments"] == [] and result["image_detection_complete"]
    assert result["empty_state_label"] == "barren_land_click_to_comment"
    assert result["capture_complete_source"] == "visible_comment_empty_state"
    assert "visible_terminal_with_stale_declared_count" in result["warnings"]

    for html, entity in [
        (root + region + end, "another"),
        (region, "sample"),
        (root + region + region + end, "sample"),
        ((root + region + end) * 2, "sample"),
        (root + region.replace("这是一片荒地", "加载中") + end, "sample"),
        (root + region.replace('class="no-comments"', 'class="no-comments" hidden') + end, "sample"),
        (root + region.replace('</div>', '<button>展开2条回复</button></div>', 1) + end, "sample"),
        (root + region.replace('</div>', '<div class="comment-item" hidden>later</div></div>', 1) + end, "sample"),
        (root + '<span role="progressbar">加载中</span>' + region + end, "sample"),
        (root + region + '<div class="error">笔记不存在</div>' + end, "sample"),
        (root + region + ordinary + end, "sample"),
    ]:
        show(html, entity)
        assert not collector.stable_visible_empty_state(actual, "sample", [], 0)

    show(root + region + end)
    assert not collector.stable_visible_empty_state(actual, "sample", [{"subCommentCount":2}], 2)
    for wrapped in ({"list":[], "hasMore":True}, {"data":{"list":[], "has_more":True}}):
        page.evaluate("comments => window.__INITIAL_STATE__.note.noteDetailMap.sample.comments = comments", wrapped)
        assert not collector.stable_visible_empty_state(actual, "sample", [], 2)
    show(root + '<div style="height:2400px"></div>' + region + end)
    page.evaluate("""html => window.addEventListener('scroll', () => setTimeout(() => {
        document.querySelector('.comments-el').innerHTML = html;
    }, 150), {once:true})""", ordinary)
    result, terminal, _ = capture(2)
    assert terminal and [c["text"] for c in result["comments"]] == ["wanted"], result
    assert result["capture_complete_source"] == "visible_comment_list_end"
    page.goto("about:blank")
