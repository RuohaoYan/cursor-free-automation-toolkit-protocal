from __future__ import annotations

import asyncio

from modules.cursor_browser_flow import CursorBrowserFlow


class _FakePage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.url = "https://authenticator.cursor.sh/password"

    async def evaluate(self, script: str, *args: object) -> str:
        return self.text


def test_is_human_verification_blocked_detects_chinese_message() -> None:
    flow = CursorBrowserFlow(_FakePage("无法验证用户为真人，请稍后再试"), "[test]")

    blocked = asyncio.run(flow.is_human_verification_blocked())

    assert blocked is True


def test_is_human_verification_blocked_false_for_normal_page() -> None:
    flow = CursorBrowserFlow(_FakePage("Email code sign in"), "[test]")

    blocked = asyncio.run(flow.is_human_verification_blocked())

    assert blocked is False


def test_is_phone_verification_rate_limited_detects_chinese_message() -> None:
    flow = CursorBrowserFlow(
        _FakePage("此手机号码的验证请求过多，请联系您的管理员。"),
        "[test]",
    )

    limited = asyncio.run(flow.is_phone_verification_rate_limited())

    assert limited is True


def test_is_phone_verification_rate_limited_false_for_normal_phone_page() -> None:
    flow = CursorBrowserFlow(
        _FakePage("Verify your phone number"),
        "[test]",
    )

    limited = asyncio.run(flow.is_phone_verification_rate_limited())

    assert limited is False
