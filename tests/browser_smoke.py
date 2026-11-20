"""Real installed Chrome + persistent profile smoke, only about:blank, no account."""
import tempfile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "comment-review-audit" / "scripts"))
from browser_environment import find_chrome
from profile_lock import ProfileLock
from session_environment import network_fingerprint
from xhs_persistent import persistent_client
from playwright.sync_api import sync_playwright
from xhs_cli.auth import REQUIRED_COOKIES, cookie_str_to_dict, get_cookie_string, save_cookies
from xhs_cli.client import XhsClient
from xhs_visible_login import load_xhs_modules as login_modules
from xhs_batch_capture import expand_dom_comments, load_xhs_modules as capture_modules
from test_xhs_batch_capture import exercise_real_reply_recheck, exercise_visible_comments
import xhs_batch_capture
from deferred_comments import exercise_deferred_comments
from recheck_dom import exercise_recheck_dom

assert login_modules()[-1] is XhsClient
assert capture_modules()[2] is XhsClient

chrome = find_chrome()
fingerprint = network_fingerprint()
assert fingerprint != "unavailable", "network monitor unavailable on this runner"
assert len(fingerprint) == 64
with tempfile.TemporaryDirectory(prefix="audit-browser-") as directory:
    profile = Path(directory) / "中文 空格 Chrome 会话"
    for iteration in range(2):
        with ProfileLock(profile):
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(profile), executable_path=chrome, headless=True)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto("about:blank")
                page.set_content("<p>图字成功😀</p>")
                assert page.locator("p").inner_text() == "图字成功😀"
                page.set_content("""
                    <div class="comments-container">
                      <div class="comment-item" data-comment-id="parent">
                        <div class="content"><span class="note-text">First</span></div>
                        <div class="comment-item" data-comment-id="child" data-target-comment-id="parent">
                          <div class="content"><span class="note-text">Reply</span></div>
                          <span class="comment-picture"><img></span>
                        </div>
                      </div>
                      <div>没有更多评论</div>
                    </div>""")
                rows, terminal, parsed = expand_dom_comments(page, max_rounds=0)
                assert terminal and parsed
                assert [r["text"] for r in rows] == ["First", "Reply"]
                assert not rows[0]["has_image"] and rows[1]["has_image"]
                assert "parent_comment_id" not in rows[0]
                assert rows[1]["parent_comment_id"] == rows[0]["comment_id"]
                if iteration == 0:
                    # Playwright regex locators see the raw surrounding whitespace in this footer.
                    for footer in (" - THE END - ", "\n　没有更多评论\t"):
                        page.set_content(f'<div class="comments-container"><div class="end-container">{footer}</div></div>')
                        assert xhs_batch_capture.visible_terminal(page.locator(".comments-container"))
                    page.set_content('<div class="comments-container"><div class="comment-item">'
                                     '<div class="content"> - THE END - </div></div></div>')
                    assert not xhs_batch_capture.visible_terminal(page.locator(".comments-container"))
                    page.set_content('<div class="comments-container"><button> 展开2条回复 </button>'
                                     '<div class="end-container"> - THE END - </div></div>')
                    assert xhs_batch_capture.reply_controls(page.locator(".comments-container")).count() == 1
                    exercise_real_reply_recheck(page)
                    exercise_visible_comments(page)
                    exercise_deferred_comments(page, xhs_batch_capture)
                    exercise_recheck_dom(page, xhs_batch_capture)
                context.close()
    # Exercise the actual supported XHS client interface with our Chrome wrapper.
    client = persistent_client(XhsClient, {}, str(Path(directory) / "xhs 测试"),
                               False, initial_url="about:blank")
    try:
        client.start()
        assert client._page.url == "about:blank"
    finally:
        client.close()
    assert client._audit_profile_lock._handle is None
print("Real Chrome, persistent profiles, network monitor and XHS wrapper passed; no live login tested.")
