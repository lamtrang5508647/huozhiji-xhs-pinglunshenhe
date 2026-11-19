#!/usr/bin/env python3
"""Offline tests for the Xiaohongshu observation normalizer."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from xhs_capture import (  # noqa: E402
    comments_exhausted,
    classify_failure,
    decode_json_output,
    find_comments,
    flatten_comments,
    has_more,
    normalize_comment,
    normalized_comments,
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
    assert comments_exhausted(payload, comments)
    assert not comments_exhausted({"comments":comments}, comments)
    assert not comments_exhausted(comments, comments)
    for flag in ({"subCommentHasMore":True}, {"sub_comment_count":"2","sub_comments":[{"id":"one"}]}):
        missing = {"comments":[{"id":"root", **flag}],"hasMore":False}
        assert not comments_exhausted(missing, missing["comments"])
    assert decode_json_output("log line\n" + __import__("json").dumps(payload)) == payload
    assert classify_failure(1, "请先登录") == "session_expired"
    assert classify_failure(1, "captcha required") == "captcha"
    assert classify_failure(1, "risk control") == "risk_control"
    assert classify_failure(1, "安全限制：IP存在风险（300012）") == "risk_control"
    assert classify_failure(1, "redirected to /website-login/error") == "risk_control"
    nested = [{"id": "p", "content": "父评论", "subComments": [{"id": "r", "content": "回复"}]}]
    assert [item["id"] for item in flatten_comments(nested)] == ["p", "r"]
    thread = [{"id": "root", "subComments": [
        {"id": "first", "target_comment": {"id": "root"}},
        {"id": "second", "targetComment": {"commentId": "first"}},
        {"id": "unknown", "rootCommentId": "root"},
        {"id": "unidentified", "targetComment": {"userId": "private"}},
        {"id": "conflict", "targetCommentId": "first", "parent_comment_id": "root"},
    ]}]
    normalized = normalized_comments(thread, root_list=True)
    assert [item.get("parent_comment_id", "missing") for item in normalized] == [
        None, "root", "first", "missing", "missing", "missing"]
    assert "parent_comment_id" not in normalized_comments(thread)[0]
    parent = {"id":"root", "content":"A question"}
    target = {"id":"reply", "content":"Answer", "targetComment":{"content":"A question"}}
    rows = normalized_comments([parent, target], root_list=True)
    assert rows[1]["parent_text"] == "A question" and "parent_comment_id" not in rows[1]
    assert normalize_comment({**target, "targetComment":parent})["parent_comment_id"] == "root"
    for conflicting in (
        {"targetComment":parent, "parentCommentId":"other"},
        {"targetComment":{"content":"原评论已删除"}},
        {"targetComment":{"isDeleted":True, "content":"A question"}},
        {"targetComment":{"content":"A question"}, "parentComment":{"content":"Other"}},
        {"targetComment":{"id":"reply","content":"A question"}},
        {"targetComment":{"userInfo":{"nickname":"A question"}}},
        {"rootComment":{"content":"A question"}},
    ):
        record = normalize_comment({"id":"reply", "content":"Answer", **conflicting})
        assert "parent_text" not in record and "parent_comment_id" not in record
    print("ok: xhs capture adapter regression tests")


if __name__ == "__main__":
    run()
