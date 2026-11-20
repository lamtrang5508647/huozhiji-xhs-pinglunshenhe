#!/usr/bin/env python3
"""Offline tests for target identity, visible text fallback and count hints.

The real Chrome cases in tests/douyin_visibility.py exercise rendered attachments,
structured bodies, nested replies, loading indicators and completion markers.
"""
from douyin_batch_capture import on_assigned_video, parse_item_lines, parse_declared_comment_count


def main():
    assert parse_item_lines("昵称\n...\n这是评论正文\n3小时前·广东\n0\n分享\n回复") == "这是评论正文"
    assert parse_item_lines("昵称\n1天前\n回复") == ""
    assert parse_item_lines("未知布局\n展开回复") == ""
    assert parse_declared_comment_count("9") == 9
    assert parse_declared_comment_count("抢首评") == 0
    assert parse_declared_comment_count("") is None
    sample = {"url": "https://www.douyin.com/video/123", "target_key": "123"}
    assert on_assigned_video("https://www.douyin.com/?modal_id=123", sample)
    assert not on_assigned_video("https://www.douyin.com/video/456", sample)
    assert not on_assigned_video("https://www.douyin.com/", sample)
    assert not on_assigned_video("https://douyin.com.invalid/video/123", sample)
    short = {"url": "https://v.douyin.com/aBcD/", "target_key": "aBcD"}
    assert on_assigned_video("https://www.douyin.com/video/123", short)
    print("douyin batch capture tests passed")


if __name__ == "__main__":
    main()
