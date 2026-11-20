"""Local Chrome regression cases for stopping a positive second-pass check."""
from argparse import Namespace
from datetime import datetime, timezone


def exercise_recheck_dom(page, collector):
    def card(identifier, text, image=False):
        attribute = f' data-comment-id="{identifier}"' if identifier else ""
        picture = '<span class="comment-picture"><img></span>' if image else ""
        return f'<div class="comment-item"{attribute}><div class="content"><span class="note-text">{text}</span></div>{picture}</div>'

    row = dict(platform="xiaohongshu", target_key="note", case_id="row", expected_amount="3.75",
               comment_text="target", comment_type="text", prior_review={"triage": "成功"})
    other = dict(row, case_id="second")

    def capture(tasks, visible, more, *, rounds=2, strict=False):
        page.goto("about:blank")
        page.set_content('<div class="comments-container"><div class="parent-comment">' + visible
                         + '<div id="replies"></div><button onclick="window.clicked=true;this.remove();'
                         'setTimeout(() => replies.append(next.content.cloneNode(true)), 150)">展开1条回复</button>'
                         + '</div><span>没有更多评论</span></div><template id="next">' + more + '</template>')
        args = Namespace(expand_dom=True, dom_max_rounds=rounds, incomplete_dom_retry_rounds=2,
                         require_reply_parent=strict)
        observed, _, _ = collector.check_note_comments(page, "note", [], None, tasks, args, context_rows=tasks)
        results = collector.compare(tasks, [observed], amount_mode="expected-only", require_reply_parent=strict)["results"]
        return observed, results, bool(page.evaluate("window.clicked"))

    active_card, next_card = card("first", "target"), card("other", "unused")
    hit, results, clicked = capture([row], active_card, next_card)
    assert not clicked and hit["capture_stop_reason"] == "recheck_matches_visible" and not hit["capture_complete"]
    assert results[0]["approved_amount"] == "3.75"
    hit.update(captured_at=datetime.now(timezone.utc).isoformat(), parser_revision=collector.PARSER_REVISIONS["xiaohongshu"])
    assert collector.capture_covers_tasks(hit, "note", [row])
    assert not collector.capture_covers_tasks(hit, "note", [row, other])

    # Two assigned rows with one visible copy cannot yet be paid independently.
    hit, results, clicked = capture([row, other], active_card, card("second", "target"))
    assert clicked and all(r["approved_amount"] == "3.75" for r in results)
    assert len({r["matched_comment"]["comment_id"] for r in results}) == 2

    # The reply appears only when the second DOM pass opens the thread.
    combo = dict(row, comment_type="text+image", has_image=True, partial_text_amount="1")
    hit, results, clicked = capture([combo], active_card, card("combo", "target", True), rounds=0)
    assert clicked and results[0]["status"] == "verified" and results[0]["approved_amount"] == "3.75"
    assert results[0]["matched_comment"]["comment_id"] == "combo"

    # Distinct ids, even for matching bodies, and an observed attachment protect payment.
    bare = dict(row, comment_type="image", comment_text="", has_image=True)
    for tasks, visible, more, strict in (
        ([bare], card("a", "", True), card("b", "", True), False),
        ([row], card(None, "target"), next_card, False),
        ([dict(row, expected_amount=None)], active_card, next_card, False),
        ([{k:v for k,v in row.items() if k != "prior_review"}], active_card, next_card, False),
        ([row], active_card, next_card, True),
    ):
        hit, _, clicked = capture(tasks, visible, more, strict=strict)
        assert clicked and hit.get("capture_stop_reason") is None

    # The note's initial state and the actually rendered comment list can disagree.
    cached = [{"id":"old", "content":"target"}]
    args = Namespace(expand_dom=True, dom_max_rounds=0, incomplete_dom_retry_rounds=0)
    for visible, verdict in ((card("new", "different"), "待人审"),
                             (card("new", "different") + "<span>没有更多评论</span>", "失败"),
                             (card("new", "target"), "成功")):
        page.set_content('<div class="comments-container">' + visible + '</div>')
        observed, _, _ = collector.check_note_comments(page, "note", cached, 1, [row], args)
        verdicts = collector.compare([row], [observed], amount_mode="expected-only")["results"]
        assert verdicts[0]["triage"] == verdict
        assert all(record["comment_id"] != "old" for record in observed["comments"])
