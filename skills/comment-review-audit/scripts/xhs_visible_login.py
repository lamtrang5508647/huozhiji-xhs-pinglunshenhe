#!/usr/bin/env python3
"""Open a visible Xiaohongshu login page and persist a newly scanned session."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from xhs_persistent import persistent_client


def load_xhs_modules():
    try:
        from xhs_cli.auth import REQUIRED_COOKIES, cookie_str_to_dict, get_cookie_string, save_cookies
        from xhs_cli.client import XhsClient
        return REQUIRED_COOKIES, cookie_str_to_dict, get_cookie_string, save_cookies, XhsClient
    except ImportError:
        candidates = sorted((Path.home() / ".local/share/uv/tools/xhs-cli/lib").glob("python*/site-packages"))
        if not candidates:
            raise RuntimeError("xhs_cli_not_installed")
        # Keep the tool environment available for xhs_cli without shadowing
        # this Python's compiled dependencies such as Playwright/greenlet.
        sys.path.append(str(candidates[-1]))
        from xhs_cli.auth import REQUIRED_COOKIES, cookie_str_to_dict, get_cookie_string, save_cookies
        from xhs_cli.client import XhsClient
        return REQUIRED_COOKIES, cookie_str_to_dict, get_cookie_string, save_cookies, XhsClient


def visible_login_gate(page) -> bool:
    for marker in ("手机号登录", "输入验证码", "登录后推荐更懂你的笔记", "登录后查看更多评论"):
        try:
            locator = page.get_by_text(marker, exact=False)
            if locator.count() and locator.first.is_visible():
                return True
        except Exception:
            continue
    return False


def verification_status(client, error: Exception) -> str:
    """Classify a blocker without logging its URL, tokens, or exception payload."""
    text = (str(error) + " " + str(getattr(getattr(client, "_page", None), "url", ""))).casefold()
    if any(marker in text for marker in ("risk control", "risk_control", "300012", "安全限制", "ip存在风险", "website-login/error", "频繁")):
        return "risk_control"
    if any(marker in text for marker in ("captcha", "verification", "安全验证", "请完成验证", "验证码")):
        return "captcha"
    if "login required" in text or "登录" in text:
        return "session_expired"
    return "risk_control"


def supported_url(value: str) -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or not (
        host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")
        or host in ("xhslink.com", "xhslink.cn", "www.xhslink.com", "www.xhslink.cn")
    ):
        raise argparse.ArgumentTypeError("--url must be an official Xiaohongshu or xhslink URL")
    return value


def wait_for_owner_confirmation(client, required_cookies, save_cookies, confirm=input) -> None:
    """Do not close or refresh a verification page while its owner is working."""
    from xhs_cli.exceptions import LoginError
    while True:
        print("VERIFICATION_WINDOW_READY — waiting for owner confirmation; no automatic close", flush=True)
        confirm()
        try:
            client._raise_if_blocked("checking owner-completed verification", include_body=True)
        except LoginError:
            print("VERIFICATION_STILL_REQUIRED — window remains open", flush=True)
            continue
        current = {item["name"]: item["value"] for item in client._page.context.cookies()}
        if visible_login_gate(client._page) or not required_cookies.issubset(current):
            print("LOGIN_STILL_REQUIRED — window remains open", flush=True)
            continue
        save_cookies("; ".join(f"{key}={value}" for key, value in current.items()))
        print("LOGIN_SESSION_SAVED", flush=True)
        return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--profile", default=os.path.expanduser("~/.comment-review-audit/xhs-profile"))
    parser.add_argument("--url", type=supported_url, default="https://www.xiaohongshu.com",
                        help="Open the original target or verification page in this same profile")
    parser.add_argument("--wait-for-confirmation", action="store_true",
                        help="Keep verification/login visible until owner confirmation via stdin")
    args = parser.parse_args()
    REQUIRED_COOKIES, cookie_str_to_dict, get_cookie_string, save_cookies, XhsClient = load_xhs_modules()
    original = cookie_str_to_dict(get_cookie_string() or "")
    client = persistent_client(XhsClient, original, args.profile, True, initial_url=args.url)
    try:
        from xhs_cli.exceptions import LoginError
        try:
            client.start()
        except LoginError:
            if not args.wait_for_confirmation or not getattr(client, "_page", None):
                raise
            # The startup challenge stays open and retains its profile lock.
        page = client._page
        if args.wait_for_confirmation:
            page.bring_to_front()
            wait_for_owner_confirmation(client, REQUIRED_COOKIES, save_cookies)
            return 0
        print("LOGIN_WINDOW_READY — scan the visible QR code", flush=True)
        deadline = time.time() + args.timeout
        healthy_seconds = 0
        while time.time() < deadline:
            current = {item["name"]: item["value"] for item in page.context.cookies()}
            login_visible = visible_login_gate(page)
            try:
                client._raise_if_blocked("checking visible session", include_body=True)
                blocked = False
            except LoginError:
                blocked = True
            healthy_seconds = healthy_seconds + 1 if REQUIRED_COOKIES.issubset(current) and not login_visible and not blocked else 0
            if healthy_seconds >= 3:
                time.sleep(2)
                current = {item["name"]: item["value"] for item in page.context.cookies()}
                save_cookies("; ".join(f"{key}={value}" for key, value in current.items()))
                print("LOGIN_SESSION_SAVED", flush=True)
                return 0
            time.sleep(1)
        print("LOGIN_TIMEOUT", flush=True)
        return 2
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
