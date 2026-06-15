from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from modules.cursor_browser_flow import CursorBrowserFlow


class _EmailInput:
    def __init__(self, value: str = "") -> None:
        self._value = value
        self.press = AsyncMock()

    async def input_value(self) -> str:
        return self._value


class _AuthEmailPage:
    def __init__(self, *, advanced_after_submit: bool = False) -> None:
        self.advanced_after_submit = advanced_after_submit
        self.url = "https://authenticator.cursor.sh/"
        self.evaluate = AsyncMock(return_value=True)

    async def wait_for_timeout(self, ms: int) -> None:
        return None


def test_fill_auth_email_advances_after_js_submit(monkeypatch) -> None:
    page = _AuthEmailPage(advanced_after_submit=True)
    flow = CursorBrowserFlow(page, "[test]")
    email_input = _EmailInput()

    async def fake_find(_selectors):
        return email_input

    calls = {"auth_email": 0}

    async def fake_is_auth_email_step() -> bool:
        calls["auth_email"] += 1
        return calls["auth_email"] <= 1

    monkeypatch.setattr(flow, "_find_any_input", fake_find)
    monkeypatch.setattr(flow, "_human_fill", AsyncMock())
    monkeypatch.setattr(flow, "_is_auth_email_step", fake_is_auth_email_step)
    monkeypatch.setattr(flow, "click_submit", AsyncMock(return_value=False))
    monkeypatch.setattr(flow, "_click_by_labels", AsyncMock(return_value=False))
    monkeypatch.setattr(flow, "_submit_email_form_js", AsyncMock(return_value=True))
    monkeypatch.setattr(flow, "_settle", AsyncMock())

    asyncio.run(flow.fill_auth_email("user@example.com"))

    email_input.press.assert_not_called()


def test_fill_auth_email_uses_enter_when_buttons_missing(monkeypatch) -> None:
    page = _AuthEmailPage()
    flow = CursorBrowserFlow(page, "[test]")
    email_input = _EmailInput()

    async def fake_find(_selectors):
        return email_input

    calls = {"auth_email": 0}

    async def fake_is_auth_email_step() -> bool:
        calls["auth_email"] += 1
        return calls["auth_email"] <= 1

    monkeypatch.setattr(flow, "_find_any_input", fake_find)
    monkeypatch.setattr(flow, "_human_fill", AsyncMock())
    monkeypatch.setattr(flow, "_is_auth_email_step", fake_is_auth_email_step)
    monkeypatch.setattr(flow, "click_submit", AsyncMock(return_value=False))
    monkeypatch.setattr(flow, "_click_by_labels", AsyncMock(return_value=False))
    monkeypatch.setattr(flow, "_submit_email_form_js", AsyncMock(return_value=False))
    monkeypatch.setattr(flow, "_settle", AsyncMock())

    asyncio.run(flow.fill_auth_email("user@example.com"))

    email_input.press.assert_called_once_with("Enter")
