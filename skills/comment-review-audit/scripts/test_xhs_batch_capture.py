#!/usr/bin/env python3
"""Offline regression tests for XHS DOM expansion parsing and completeness."""

from argparse import Namespace

from xhs_batch_capture import apply_risk_averse_profile, dom_capture_complete, parse_dom_comment
from xhs_batch_capture import target_from_url, visible_terminal, expand_dom_comments


class FakeLocator:
    def __init__(self, count: int):
        self._count = count

    def count(self) -> int:
        return self._count


class FakeItem:
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


def main() -> None:
    assert parse_dom_comment(FakeReplyItem('请问喝哪家啊？'))['text'] == '请问喝哪家啊？'
    assert parse_dom_comment(FakeReplyItem('回复 小明：这是作者自己写的正文'))['text'] == '回复 小明：这是作者自己写的正文'
    long_target = target_from_url('标题 https://www.xiaohongshu.com/explore/n1?xsec_token=fixture&xsec_source=pc_ad')
    assert long_target['note_id'] == 'n1' and long_target['token'] == 'fixture'
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
            from types import SimpleNamespace
            return SimpleNamespace(count=lambda: 1, first=SimpleNamespace(is_visible=lambda: self.visible))
    assert not visible_terminal(Terminal(False))
    assert visible_terminal(Terminal(True))
    from types import SimpleNamespace
    class Items:
        def __init__(self, values):
            self.values = values
            self.last = SimpleNamespace(scroll_into_view_if_needed=lambda **kw: None)
        def count(self):
            return len(self.values)
        def nth(self, index):
            return self.values[index]
    class Container(Terminal):
        first = property(lambda self: self)
        def is_visible(self):
            return True
        def count(self):
            return 1
        def locator(self, selector):
            return self.items
        def get_by_text(self, marker, **kw):
            return super().get_by_text(marker, **kw) if isinstance(marker, str) else FakeLocator(0)
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
    assert dom_capture_complete(6, 6, False)
    assert dom_capture_complete(7, 6, True)  # stale count but visible THE END
    assert not dom_capture_complete(7, 6, False)
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
