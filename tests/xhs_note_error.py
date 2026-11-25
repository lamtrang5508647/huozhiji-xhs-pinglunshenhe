"""Substitute local responses for an authorized note's short-lived official 404 and its home redirect."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import urlparse
import json
import time


def exercise_note_error(page, reader):
    source = "https://www.xiaohongshu.com/explore/demo?xsec_source=fixture"
    error = "https://www.xiaohongshu.com/404?noteId=demo&errorCode=-510001"
    home = "https://www.xiaohongshu.com/explore?source=fixture"
    prompt = '<h1>你访问的页面不见了</h1>'
    class FetchError(Exception):
        pass
    class LoginError(Exception):
        pass
    class Client:
        _page = page
        _audit_note_id = "demo"
        _audit_source_url = source
        def __init__(self, _cookies):
            pass
        def _human_wait(self, minimum, maximum):
            time.sleep(maximum)
        def get_note_detail(self, *_):
            page.goto(source, wait_until="domcontentloaded")
            if goes_home:
                # Match xhs-cli handing control back only after its pacing delay.
                self._human_wait(1.25, 1.25)
            page.wait_for_url(home if goes_home else landing, wait_until="domcontentloaded", timeout=6000)
            return {"note":{"noteId":"demo"}}
        def get_note_comments(self, *args, **kwargs):
            return []
    examples = [
        (error, prompt, True, True, "inaccessible"),
        (error.replace("noteId=demo","noteId=foreign"), prompt, False, True, "wrong_note"),
        (error, prompt, False, False, "wrong_note"),
        (error, '<div class="note-container">'+prompt+'</div>', False, True, "wrong_note"),
        (error, prompt+'<div style="position:fixed;inset:0;z-index:5;background:white">稍候</div>',
         False, True, "wrong_note"),
        (error, '<div>系统正忙，请稍后重试</div>', False, True, "wrong_note"),
        (error, prompt+'<div role="dialog">请完成安全验证</div>', False, True, "security"),
    ]
    with TemporaryDirectory() as temp:
        client = reader.persistent_client(Client, {}, temp, False)
        folder = Path(temp)/"评论截图"
        for landing, content, goes_home, authorized, desired in examples:
            client._audit_source_url = source if authorized else source.replace("/demo","/other")
            def serve(route):
                path = urlparse(route.request.url).path
                if path == "/explore/demo":
                    html = '<script>setTimeout(()=>location.replace('+json.dumps(landing)+'),40)</script>'
                elif path == "/404":
                    html = content + ('<script>setTimeout(()=>location.replace('+json.dumps(home)+'),1000)</script>' if goes_home else '')
                else:
                    html = '<main>首页</main>'
                route.fulfill(status=200,content_type="text/html; charset=utf-8",body=html)
            page.route("**/*",serve)
            previous = set(folder.rglob("*.png"))
            try:
                args = SimpleNamespace(screenshot_dir=folder,expand_dom=True,dom_max_rounds=1,incomplete_dom_retry_rounds=0)
                obs, *_ = reader.collect_note_evidence(client,"demo","demo","",[],args,LoginError,FetchError)
                assert desired == "inaccessible" and obs["capture_status"] == "inaccessible" and not obs["capture_complete"]
                assert obs["target_page_state"] == "post_unavailable" and obs["availability_source"] == "official_note_error_page"
                assert obs["comments"] == [] and obs["screenshot_status"] == "partial" and page.url == home
                proof = obs["comment_screenshots"]
                assert len(proof) == 1 and proof[0]["kind"] == "unavailable" and (Path(temp)/proof[0]["path"]).is_file()
                task = dict(case_id="a",platform="xiaohongshu",target_key="demo",comment_text="wanted",expected_amount="1")
                assert reader.compare([task],[obs],amount_mode="expected-only")["results"][0]["triage"] == "待人审"
            except FetchError as exc:
                assert desired == "wrong_note" and str(exc) == "note_identity_unconfirmed"
            except LoginError:
                assert desired == "security"
            finally:
                page.unroute("**/*",serve)
            if desired != "inaccessible":
                assert set(folder.rglob("*.png")) == previous
    page.goto("about:blank")
