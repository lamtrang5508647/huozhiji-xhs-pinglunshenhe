#!/usr/bin/env python3
"""Offline regression tests for Douyin desktop comment parsing."""

from douyin_batch_capture import (
    detect_comment_image,
    parse_comment_item,
    parse_declared_comment_count,
    should_stop_loading,
)


class FakeItem:
    def __init__(self, text: str):
        self.text = text

    def inner_text(self, timeout: int = 0) -> str:
        return self.text

    def locator(self, selector):
        return FakeLocator(0)


class StructuredItem(FakeItem):
    def __init__(self, body):
        super().__init__('昵称\n...\n'+body+'\n作者赞过\n3小时前·广东\n0\n分享\n回复')
        self.body=body

    def locator(self, selector):
        class Body:
            def count(inner): return 1
            @property
            def first(inner): return inner
            def evaluate(inner, script): return self.body
        return Body()


class FakeLocator:
    def __init__(self, count: int = 0, evaluated=False, fails: bool = False):
        self._count = count
        self._evaluated = evaluated
        self._fails = fails

    def count(self) -> int:
        if self._fails:
            raise RuntimeError("unavailable")
        return self._count

    def evaluate_all(self, script: str):
        if self._fails:
            raise RuntimeError("unavailable")
        return self._evaluated


class FakeImageItem:
    def __init__(self, explicit: int = 0, evaluated=False, fails: bool = False):
        self.explicit = FakeLocator(explicit, fails=fails)
        self.images = FakeLocator(evaluated=evaluated, fails=fails)

    def locator(self, selector: str):
        return self.images if selector == "img" else self.explicit


def main() -> None:
    assert parse_comment_item(StructuredItem('[耶]入了试试')) == '[耶]入了试试'
    assert parse_comment_item(StructuredItem('评论里写了作者赞过')) == '评论里写了作者赞过'
    item = FakeItem("昵称\n...\n这是评论正文\n3小时前·广东\n0\n分享\n回复")
    assert parse_comment_item(item) == "这是评论正文"
    assert parse_declared_comment_count("9") == 9
    assert parse_declared_comment_count("抢首评") == 0
    assert parse_declared_comment_count("") is None
    assert should_stop_loading(declared=9, captured=9, terminal=False, stable=0)
    assert should_stop_loading(declared=0, captured=0, terminal=False, stable=0)
    assert should_stop_loading(declared=10, captured=9, terminal=True, stable=0)
    assert should_stop_loading(declared=10, captured=9, terminal=False, stable=4)
    assert not should_stop_loading(declared=10, captured=9, terminal=False, stable=3)
    assert detect_comment_image(FakeImageItem(explicit=1)) is True
    assert detect_comment_image(FakeImageItem(evaluated=True)) is True
    assert detect_comment_image(FakeImageItem()) is False
    assert detect_comment_image(FakeImageItem(fails=True)) is None
    print("douyin batch capture tests passed")


if __name__ == "__main__":
    main()
