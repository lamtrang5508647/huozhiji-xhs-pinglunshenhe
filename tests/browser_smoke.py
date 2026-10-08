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
from xhs_batch_capture import load_xhs_modules as capture_modules

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
