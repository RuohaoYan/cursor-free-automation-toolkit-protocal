from __future__ import annotations

import asyncio
from typing import Any

from modules import captcha_auto


def test_should_try_click_default_modes(monkeypatch) -> None:
    monkeypatch.setattr(captcha_auto, "load_env", lambda _: {"FREE_CAPTCHA_MODE": "click"})
    assert captcha_auto.should_try_click() is True
    assert captcha_auto.should_use_capsolver() is False

    monkeypatch.setattr(captcha_auto, "load_env", lambda _: {"FREE_CAPTCHA_MODE": "auto"})
    assert captcha_auto.should_try_click() is True

    monkeypatch.setattr(captcha_auto, "load_env", lambda _: {"FREE_CAPTCHA_MODE": "api"})
    assert captcha_auto.should_try_click() is False

    monkeypatch.setattr(
        captcha_auto,
        "load_env",
        lambda _: {"FREE_CAPTCHA_MODE": "auto", "CAPSOLVER_API_KEY": "key"},
    )
    assert captcha_auto.should_use_capsolver() is True

    monkeypatch.setattr(captcha_auto, "load_env", lambda _: {"FREE_CAPTCHA_MODE": "manual"})
    assert captcha_auto.should_try_click() is False
    assert captcha_auto.should_use_capsolver() is False


class _FakeLocator:
    def __init__(self, selector: str, present: bool, clicks: list[str]) -> None:
        self.selector = selector
        self.present = present
        self.clicks = clicks

    @property
    def first(self) -> "_FakeLocator":
        return self

    async def count(self) -> int:
        return 1 if self.present else 0

    async def is_visible(self, *args: Any, **kwargs: Any) -> bool:
        return self.present

    async def bounding_box(self, *args: Any, **kwargs: Any) -> dict[str, int] | None:
        if not self.present:
            return None
        return {"x": 10, "y": 10, "width": 40, "height": 40}

    async def scroll_into_view_if_needed(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def click(self, *args: Any, **kwargs: Any) -> None:
        self.clicks.append(self.selector)


class _FakeFrame:
    def __init__(self, present_selectors: set[str], clicks: list[str]) -> None:
        self.url = "https://challenges.cloudflare.com/turnstile/v0"
        self.present_selectors = present_selectors
        self.clicks = clicks

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(selector, selector in self.present_selectors, self.clicks)


class _FakeWidgetFrame(_FakeFrame):
    def __init__(self, present_selectors: set[str], clicks: list[str]) -> None:
        super().__init__(present_selectors, clicks)
        self.url = "about:blank"

    async def evaluate(self, script: str, *args: Any) -> bool:
        return True


class _FakeFrameLocator:
    def __init__(self, frame: _FakeFrame) -> None:
        self.frame = frame

    @property
    def first(self) -> "_FakeFrameLocator":
        return self

    def locator(self, selector: str) -> _FakeLocator:
        return self.frame.locator(selector)


class _FakeMouse:
    def __init__(self) -> None:
        self.clicks: list[tuple[float, float]] = []
        self.moves: list[tuple[float, float, int | None]] = []

    async def click(self, *args: Any, **kwargs: Any) -> None:
        self.clicks.append((float(args[0]), float(args[1])))
        return None

    async def move(self, *args: Any, **kwargs: Any) -> None:
        self.moves.append((float(args[0]), float(args[1]), kwargs.get("steps")))
        return None


class _FakePage:
    def __init__(self, present_selectors: set[str]) -> None:
        self.frame = _FakeFrame(present_selectors, [])
        self.frames = [self.frame]
        self.mouse = _FakeMouse()

    def frame_locator(self, selector: str) -> _FakeFrameLocator:
        return _FakeFrameLocator(self.frame)

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(selector, selector in self.frame.present_selectors, self.frame.clicks)


class _FakeTextPage:
    url = "https://auth.openai.com/"

    def __init__(self, text: str, title: str = "") -> None:
        self.text = text
        self._title = title

    async def title(self) -> str:
        return self._title

    async def evaluate(self, script: str, *args: Any) -> Any:
        if "innerText" in script:
            return self.text
        return False


class _FakeWidgetPage(_FakePage):
    def __init__(self, present_selectors: set[str]) -> None:
        self.frame = _FakeWidgetFrame(present_selectors, [])
        self.frames = [self.frame]
        self.mouse = _FakeMouse()


class _MissingLocatorPage:
    frames: list[Any] = []

    def __init__(self) -> None:
        self.mouse = _FakeMouse()
        self.waits: list[int] = []

    def frame_locator(self, selector: str) -> Any:
        raise RuntimeError("no frame locator")

    def locator(self, selector: str) -> Any:
        raise RuntimeError("no page locator")

    async def evaluate(self, script: str, *args: Any) -> Any:
        return [
            {
                "x": 100,
                "y": 200,
                "w": 300,
                "h": 65,
                "reason": "cf-turnstile-response-parent",
            }
        ]

    async def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)


def test_click_security_challenge_clicks_real_checkbox() -> None:
    page = _FakePage({'input[type="checkbox"]:not([disabled])'})
    messages: list[str] = []

    ok = asyncio.run(captcha_auto.try_click_security_challenge(page, messages.append))

    assert ok is True
    assert page.frame.clicks == ['input[type="checkbox"]:not([disabled])']
    assert any('input[type="checkbox"]' in message for message in messages)


def test_security_challenge_visible_detects_chinese_human_verification() -> None:
    page = _FakeTextPage("请验证你是真人，然后继续")

    visible, reason = asyncio.run(captcha_auto.security_challenge_visible(page))

    assert visible is True
    assert reason == "请验证你是真人"


def test_click_security_challenge_does_not_click_body_only_frame() -> None:
    page = _FakePage({"body"})
    messages: list[str] = []

    ok = asyncio.run(captcha_auto.try_click_security_challenge(page, messages.append))

    assert ok is False
    assert page.frame.clicks == []
    assert "body" not in captcha_auto._CLICK_TARGETS
    assert any("未找到可点击的验证框" in message for message in messages)


def test_is_cloudflare_interstitial_detects_cf_url() -> None:
    class _Page:
        url = "https://authenticator.cursor.sh/?__cf_chl_rt_tk=abc"

        async def evaluate(self, script: str, *args: object) -> bool:
            return False

    assert asyncio.run(captcha_auto.is_cloudflare_interstitial(_Page())) is True


def test_headed_captcha_fallback_enabled_default() -> None:
    assert captcha_auto._headed_captcha_fallback_enabled() is True


def test_click_security_challenge_clicks_visible_iframe_area() -> None:
    page = _FakePage({captcha_auto._TURNSTILE_IFRAME_SELECTORS[0]})
    messages: list[str] = []

    ok = asyncio.run(captcha_auto.try_click_security_challenge(page, messages.append))

    assert ok is True
    assert page.frame.clicks == []
    assert page.mouse.clicks == [(34.0, 30.0)]
    assert any("iframe 验证框区域" in message for message in messages)


def test_click_security_challenge_uses_turnstile_coordinate_primary() -> None:
    page = _MissingLocatorPage()
    messages: list[str] = []

    ok = asyncio.run(captcha_auto.try_click_security_challenge(page, messages.append))

    assert ok is True
    assert page.mouse.moves == [(112.0, 226.5, 8), (130.0, 232.5, 6)]
    assert page.mouse.clicks == [(130.0, 232.5)]
    assert page.waits == [120]
    assert any("cf-turnstile-response-parent" in message for message in messages)


def test_click_security_challenge_clicks_cloudflare_widget_by_dom_even_without_frame_url() -> None:
    page = _FakeWidgetPage({".cb-lb"})
    messages: list[str] = []

    ok = asyncio.run(captcha_auto.try_click_security_challenge(page, messages.append))

    assert ok is True
    assert page.frame.clicks == [".cb-lb"]
    assert any(".cb-lb" in message for message in messages)


def test_captcha_strategy_summary_includes_primary_strategy() -> None:
    summary = captcha_auto.captcha_strategy_summary()

    assert captcha_auto.CAPTCHA_CLICK_PRIMARY_STRATEGY in summary
    assert "模式=" in summary


def test_click_security_challenge_logs_primary_strategy() -> None:
    page = _MissingLocatorPage()
    messages: list[str] = []

    asyncio.run(captcha_auto.try_click_security_challenge(page, messages.append))

    assert any("第 1/" in message and captcha_auto.CAPTCHA_CLICK_PRIMARY_STRATEGY in message for message in messages)
