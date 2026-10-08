#!/usr/bin/env python3
"""Persistent ordinary-Chrome session wrapper for the XHS CLI client."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Type

from profile_lock import ProfileLock
from browser_environment import find_chrome


CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def persistent_client(
    base_class: Type[Any],
    cookie_dict: Dict[str, str],
    profile: str,
    headed: bool,
    initial_url: str = "https://www.xiaohongshu.com",
) -> Any:
    profile_path = Path(profile).expanduser().resolve()
    profile_path.mkdir(parents=True, exist_ok=True)
    os.chmod(profile_path, 0o700)

    class PersistentClient(base_class):
        def start(self) -> None:
            from playwright.sync_api import sync_playwright

            chrome = find_chrome(CHROME)
            self._audit_profile_lock = ProfileLock(profile_path)
            self._audit_profile_lock.__enter__()
            try:
                self._playwright_ctx = sync_playwright().start()
                self._browser = self._playwright_ctx.chromium.launch_persistent_context(
                    user_data_dir=str(profile_path),
                    executable_path=chrome,
                    headless=not headed,
                    viewport={"width": 1440, "height": 900},
                )
                self._page = self._browser.pages[0] if self._browser.pages else self._browser.new_page()
                cookies = [
                    {"name": key, "value": value, "domain": ".xiaohongshu.com", "path": "/"}
                    for key, value in self._cookie_dict.items()
                ]
                # Chrome's persisted session is authoritative after owner verification.
                # Bootstrap an empty profile only; a saved CLI cookie may be older.
                profile_cookies = self._browser.cookies("https://www.xiaohongshu.com")
                if cookies and not any(item.get("name") == "web_session" for item in profile_cookies):
                    self._browser.add_cookies(cookies)
                self._goto(
                    initial_url,
                    timeout=20_000,
                    wait_min=2,
                    wait_max=4,
                    context="establishing persistent browser session",
                )
            except Exception:
                # A launched browser may contain a challenge the owner must see.
                # Its profile remains locked until close(), including startup failures.
                if not getattr(self, "_browser", None):
                    self.close()
                raise

        def close(self) -> None:
            try:
                try:
                    browser = getattr(self, "_browser", None)
                    if browser:
                        browser.close()
                finally:
                    playwright_context = getattr(self, "_playwright_ctx", None)
                    if playwright_context:
                        playwright_context.stop()
            finally:
                profile_lock = getattr(self, "_audit_profile_lock", None)
                if profile_lock:
                    profile_lock.__exit__(None, None, None)

    return PersistentClient(cookie_dict)
