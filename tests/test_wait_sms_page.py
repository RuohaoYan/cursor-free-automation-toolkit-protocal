from __future__ import annotations

import asyncio

from modules.chatgpt_register import wait_for_sms_verification_page


class _FakePage:
    def __init__(self, text: str, url: str = "https://authenticator.cursor.sh/radar-challenge/send") -> None:
        self.text = text
        self.url = url

    async def wait_for_timeout(self, ms: int) -> None:
        return None


async def _body_text(_page: _FakePage) -> str:
    return _page.text


async def _short_url(url: str) -> str:
    return url


async def _no_sms(_page: _FakePage) -> bool:
    return False


async def _no_password(_page: _FakePage) -> bool:
    return False


def test_wait_for_sms_page_detects_rate_limited(monkeypatch) -> None:
    page = _FakePage("此手机号码的验证请求过多，请联系您的管理员。")
    logs: list[str] = []

    monkeypatch.setattr("modules.chatgpt_register.body_text", _body_text)
    monkeypatch.setattr("modules.chatgpt_register.short_url", _short_url)
    monkeypatch.setattr("modules.chatgpt_register.page_looks_like_sms_verification", _no_sms)
    monkeypatch.setattr("modules.chatgpt_register.page_looks_like_create_password", _no_password)

    result = asyncio.run(
        wait_for_sms_verification_page(page, logs.append, timeout=3)  # type: ignore[arg-type]
    )

    assert result == "rate_limited"
    assert any("验证请求过多" in line for line in logs)
