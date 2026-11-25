"""Exercise the Douyin collector in real Chrome against locally fulfilled official-format pages."""
from pathlib import Path
from tempfile import TemporaryDirectory

from douyin_batch_capture import capture_target
from screenshot_evidence import NoteScreenshots, screenshots_valid


def exercise_douyin_visibility(page):
    url = "https://www.douyin.com/video/1234567890"
    target = {"url": url, "target_key": "1234567890"}
    footer = '<div class="end">暂时没有更多评论</div>'
    panel = '<div data-e2e="comment-list">{}</div>'

    def row(cid, text, extra="", kind="comment-item"):
        return (f'<div data-e2e="{kind}" data-comment-id="{cid}"><div class="FduGc_lz">'
                f'<span class="Sh1Da424">{text}</span></div>{extra}</div>')

    def get(content, *, declared=1, rounds=0, wrong_id=False, writer=None):
        html = f'<main><span data-e2e="feed-comment-icon">{declared}</span>{content}</main>'
        def fulfill(route):
            if wrong_id and route.request.url == url:
                route.fulfill(status=302, headers={"Location":"https://www.douyin.com/video/999999"})
            else:
                route.fulfill(content_type="text/html; charset=utf-8", body=html)
        page.route("https://www.douyin.com/**", fulfill)
        try:
            return capture_target(page, target, rounds, writer=writer)
        finally:
            page.unroute("https://www.douyin.com/**", fulfill)

    child = row("2", "迟来的目标", '<img data-e2e="comment-picture" width="64" height="64">', "comment-reply-item")
    control = '<button onclick="this.remove();setTimeout(()=>slot.append(t.content.cloneNode(true)),220)"> 展开1条回复 </button>'
    nested = row("1", "父评", control + '<div id="slot"></div>')
    got = get(panel.format(nested + footer) + f'<template id="t">{child}</template>', rounds=2)
    assert [(c["text"], c["has_image"]) for c in got["comments"]] == [("父评",False),("迟来的目标",True)], got
    assert got["capture_complete"]
    missing = row("1", "父评", '<button onclick="this.remove()">展开1条回复</button>')
    assert not get(panel.format(missing + footer), rounds=1)["capture_complete"]
    assert not get(panel.format(row("1", "没有更多评论")), declared=1)["capture_complete"]
    assert not get(panel.format(row("1", "第一条")) + footer)["capture_complete"]
    assert not get(panel.format(footer), declared=0)["capture_complete"]
    assert not get(panel.format('<span role="progressbar">加载中</span>' + footer), declared="抢首评")["capture_complete"]
    assert get(panel.format('<p>暂无评论</p>'), declared=0)["capture_complete"]
    delayed = ('<div id="place"></div><template id="delayed">' +
               panel.format(row("1","已经出现") + footer) + '</template>' +
               '<script>setTimeout(()=>place.append(delayed.content.cloneNode(true)),250)</script>')
    assert get(delayed,declared="抢首评")["comments"][0]["text"] == "已经出现"
    visible = row("1",'<img alt="[耶]">入了试试<br>补充',
                  '<span class="comment-item-tag">作者赞过</span>')
    assert get(panel.format(visible+footer))["comments"][0]["text"] == "[耶]入了试试\n补充"
    assert get(panel.format(row("1","平常提到验证码的正文") + footer))["capture_status"] == "ok"
    gone = get('<div>该视频已删除</div>' + panel.format(row("1","推荐内容评论") + footer))
    assert not gone["capture_complete"] and not gone["comments"]
    wrong = get(panel.format(row("1","另一个视频的评论") + footer), wrong_id=True)
    assert not wrong["capture_complete"] and not wrong["comments"]
    with TemporaryDirectory() as directory:
        shots = Path(directory) / "评论截图"
        def capture(content, **kwargs):
            writer = NoteScreenshots(shots, "douyin",target["target_key"])
            result = get(content, writer=writer, **kwargs)
            return result, writer

        saved, writer = capture(panel.format(nested+footer)+f'<template id="t">{child}</template>',rounds=2)
        assert saved["capture_complete"] and not writer.failed, (saved,writer.records)
        assert [r["kind"] for r in writer.records] == ["comments","end"]
        assert writer.records[-1]["comment_ids"] == ["1","2"]
        assert writer.records[-1]["comment_texts"] == ["父评","迟来的目标"]
        assert {c["evidence_path"] for c in saved["comments"]} == {writer.records[-1]["path"]}
        observation = dict(saved,platform="douyin",target_key=target["target_key"],
                           comment_screenshots=writer.records,screenshot_status="captured")
        assert screenshots_valid(observation,shots)
        image = shots.parent/writer.records[0]["path"]
        assert image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        image.unlink()
        assert not screenshots_valid(observation,shots)

        empty, writer = capture(panel.format('<p>暂无评论</p>'),declared=0)
        assert empty["capture_complete"] and [r["kind"] for r in writer.records] == ["empty"]
        unavailable, writer = capture('<p>该视频已删除</p>'+panel.format(row("r","其他推荐内容")))
        assert unavailable["capture_status"]=="inaccessible" and not unavailable["comments"]
        assert [r["kind"] for r in writer.records]==["unavailable"] and writer.records[0]["comment_ids"]==[]
        long_panel = ('<style>[data-e2e="comment-list"]{height:145px;overflow:auto;}'
                      '[data-e2e="comment-item"]{height:53px;}</style>' +
                      panel.format("".join(row(str(i),f"评论{i}") for i in range(9))+footer))
        long_result, writer = capture(long_panel, declared=9)
        assert long_result["capture_complete"] and not writer.failed
        assert len(writer.records)>1 and writer.records[-1]["kind"]=="end"
        assert any(len(s["comment_ids"])>1 for s in writer.records), writer.records
        assert all(c.get("evidence_path") for c in long_result["comments"])
        assert {k for shot in writer.records for k in shot["comment_ids"]} == {str(i) for i in range(9)}
        for content,settings in [(panel.format(row("1","推荐评论")+footer),{"wrong_id":True}),
                                 ("<div>扫码登录</div>"+panel.format(footer),{})]:
            result,writer=capture(content,**settings)
            assert result["capture_status"]!="ok" and not writer.records
        switched = NoteScreenshots(shots,"douyin",target["target_key"])
        write = switched.save
        def switch_after_photo(*args, **kwargs):
            photo = write(*args,**kwargs)
            page.evaluate("history.replaceState({}, '', '/video/56789123')")
            return photo
        switched.save = switch_after_photo
        result = get(panel.format(row("1","切换前")+footer),writer=switched)
        assert result["capture_status"] == "inaccessible" and not result["comments"]
        assert not switched.records and not list(switched.folder.glob("*.png"))
