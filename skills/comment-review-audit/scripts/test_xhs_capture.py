#!/usr/bin/env python3
"""Offline tests for the Xiaohongshu observation normalizer."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from xhs_capture import (  # noqa: E402
    classify_failure,
    decode_json_output,
    find_comments,
    flatten_comments,
    has_more,
    normalize_comment,
)


def run():
    payload = {
        "data": {
            "comments": [
                {"commentId": "c1", "content": "已收到 99.90 元"},
                {"commentId": "c2", "content": "晒单", "imageList": [{"url": "local"}]},
            ],
            "hasMore": False,
        }
    }
    comments = find_comments(payload)
    assert comments and normalize_comment(comments[0])["text"] == "已收到 99.90 元"
    assert normalize_comment(comments[1])["has_image"] is True
    assert has_more(payload) is False
    assert decode_json_output("log line\n" + __import__("json").dumps(payload)) == payload
    assert classify_failure(1, "请先登录") == "session_expired"
    assert classify_failure(1, "captcha required") == "captcha"
    assert classify_failure(1, "risk control") == "risk_control"
    assert classify_failure(1, "安全限制：IP存在风险（300012）") == "risk_control"
    assert classify_failure(1, "redirected to /website-login/error") == "risk_control"
    nested = [{"id": "p", "content": "父评论", "subComments": [{"id": "r", "content": "回复"}]}]
    assert [item["id"] for item in flatten_comments(nested)] == ["p", "r"]
    print("ok: xhs capture adapter regression tests")


if __name__ == "__main__":
    run()
