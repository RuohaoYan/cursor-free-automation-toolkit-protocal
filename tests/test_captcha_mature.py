from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from modules import captcha_auto
from modules.turnstile_solver_client import is_local_solver_configured


def test_normalize_capsolver_proxy_from_url() -> None:
    assert (
        captcha_auto._normalize_capsolver_proxy("http://user:pass@gate.example.com:1000")
        == "gate.example.com:1000:user:pass"
    )


def test_should_use_local_solver_when_configured(monkeypatch) -> None:
    monkeypatch.setattr(
        "modules.turnstile_solver_client.is_local_solver_configured",
        lambda: True,
    )
    monkeypatch.setattr(captcha_auto, "load_env", lambda _: {"CURSOR_CAPTCHA_MODE": "auto"})
    assert captcha_auto.should_use_local_solver() is True


def test_inject_cf_cookies_adds_clearance() -> None:
    page = MagicMock()
    page.url = "https://authenticator.cursor.sh/"
    page.context.add_cookies = AsyncMock()

    asyncio.run(
        captcha_auto.inject_cf_cookies(
            page,
            {"cf_clearance": "abc123"},
            page_url=page.url,
        )
    )

    page.context.add_cookies.assert_awaited_once()
    cookies = page.context.add_cookies.await_args.args[0]
    assert cookies[0]["name"] == "cf_clearance"
    assert cookies[0]["domain"] == ".cursor.sh"


def test_local_solver_requires_sitekey_message(monkeypatch) -> None:
    page = MagicMock()
    page.url = "https://authenticator.cursor.sh/?__cf_chl_rt_tk=1"
    page.wait_for_timeout = AsyncMock()
    messages: list[str] = []

    async def fake_extract(_page):
        return {"sitekey": "", "pageurl": page.url}

    monkeypatch.setattr(captcha_auto, "extract_turnstile_info", fake_extract)

    ok = asyncio.run(captcha_auto._try_local_turnstile_solver(page, messages.append))

    assert ok is False
    assert any("无 sitekey" in m for m in messages)


def test_is_local_solver_configured_false_by_default(monkeypatch) -> None:
    monkeypatch.setattr("modules.utils.load_env", lambda _: {})
    assert is_local_solver_configured() is False
