"""Regression: persisted verification wins over stale cookies, including startup stops."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xhs_persistent import persistent_client


class Base:
    def __init__(self, cookies):
        self._cookie_dict = cookies

    def _goto(self, url, **kwargs):
        self.visited = url
        if getattr(self, "fail", False):
            raise RuntimeError("synthetic startup challenge")


def main():
    for stored in ([], [{"name": "web_session", "value": "new-owner-session"}]):
        added = []
        page = SimpleNamespace()
        browser = SimpleNamespace(pages=[page], cookies=lambda url: stored,
                                  add_cookies=added.extend, close=lambda: None)
        runtime = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=lambda **kw: browser),
                                  stop=lambda: None)
        fake = SimpleNamespace(sync_playwright=lambda: SimpleNamespace(start=lambda: runtime))
        with tempfile.TemporaryDirectory() as folder, patch.dict("sys.modules", {"playwright.sync_api": fake}), patch("xhs_persistent.CHROME", __file__):
            client = persistent_client(Base, {"web_session": "old-cli-session"}, folder, True,
                                       initial_url="https://www.xiaohongshu.com/explore/fixture")
            client.start()
            assert client.visited.endswith("/fixture")
            assert bool(added) is (not bool(stored))
            client.close()
            client.fail = True
            try:
                client.start()
            except RuntimeError:
                assert client._audit_profile_lock._handle is not None
                assert client._page is page
            else:
                raise AssertionError("startup challenge was suppressed")
            client.close()
            assert client._audit_profile_lock._handle is None
    print("persistent session regression tests passed")


if __name__ == "__main__":
    main()
