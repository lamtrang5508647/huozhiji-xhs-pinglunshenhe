#!/usr/bin/env python3
"""Open a persistent visible Chrome profile and wait for Douyin login."""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from profile_lock import ProfileLock
from douyin_batch_capture import security_status, sync_playwright
from browser_environment import find_chrome


CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def wait_for_owner_confirmation(page, context, profile):
    while True:
        print("DOUYIN_VERIFICATION_WINDOW_READY — waiting for owner; no automatic close", flush=True)
        input()
        if security_status(page):
            print("DOUYIN_VERIFICATION_STILL_REQUIRED — window remains open", flush=True)
            continue
        context.storage_state(path=str(profile / "storage-state.json"))
        print("DOUYIN_LOGIN_SESSION_SAVED", flush=True)
        return


def visible_login_gate(page) -> bool:
    markers = ("登录后免费畅享高清视频", "扫码登录", "验证码登录", "密码登录")
    for marker in markers:
        try:
            locator = page.get_by_text(marker, exact=False)
            if locator.count() and locator.first.is_visible():
                return True
        except Exception:
            continue
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--profile", default=str(Path.home() / ".comment-review-audit/douyin-profile"))
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--wait-for-confirmation", action="store_true")
    args = parser.parse_args()
    profile = Path(args.profile)
    profile.mkdir(parents=True, exist_ok=True)
    os.chmod(profile, 0o700)
    with ProfileLock(profile):
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                executable_path=find_chrome(CHROME),
                headless=False,
                viewport={"width": 1440, "height": 900},
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(args.url, wait_until="domcontentloaded", timeout=30_000)
                print("DOUYIN_LOGIN_WINDOW_READY", flush=True)
                if args.wait_for_confirmation:
                    wait_for_owner_confirmation(page, context, profile)
                    return 0
                healthy_seconds = 0
                deadline = time.time() + args.timeout
                while time.time() < deadline:
                    healthy_seconds = healthy_seconds + 1 if not security_status(page) else 0
                    if healthy_seconds >= 5:
                        context.storage_state(path=str(profile / "storage-state.json"))
                        print("DOUYIN_LOGIN_SESSION_SAVED", flush=True)
                        return 0
                    time.sleep(1)
                print("DOUYIN_LOGIN_TIMEOUT", flush=True)
                return 2
            finally:
                context.close()


if __name__ == "__main__":
    raise SystemExit(main())
