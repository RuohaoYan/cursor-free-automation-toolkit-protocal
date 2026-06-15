from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modules.cursor_browser_flow import CursorBrowserFlow


class FakeLocator:
    def __init__(self, *, count: int = 1, visible: bool = True, editable: bool = True) -> None:
        self._count = count
        self._visible = visible
        self._editable = editable
        self.filled: list[str] = []

    async def count(self) -> int:
        return self._count

    async def is_visible(self, timeout: int = 0) -> bool:  # noqa: ARG002
        return self._visible

    async def evaluate(self, script: str, arg: Any = None) -> bool:  # noqa: ARG002
        return self._editable

    async def fill(self, value: str) -> None:
        self.filled.append(value)

    async def scroll_into_view_if_needed(self, timeout: int = 0) -> None:  # noqa: ARG002
        return None

    async def click(self, timeout: int = 0) -> None:  # noqa: ARG002
        return None

    async def type(self, value: str, delay: int = 0) -> None:  # noqa: ARG002
        self.filled.append(value)


class FakeLocatorChain:
    def __init__(self, locator: FakeLocator) -> None:
        self._locator = locator

    @property
    def first(self) -> FakeLocator:
        return self._locator


def _make_page(*, indexed: list[FakeLocator] | None = None, fallback: FakeLocator | None = None) -> MagicMock:
    indexed = indexed or []
    fallback = fallback or FakeLocator(count=0, editable=False)
    page = MagicMock()

    def locator(selector: str) -> FakeLocatorChain:
        if selector.startswith('input[data-index="'):
            idx = int(selector.split('"')[1])
            if idx < len(indexed):
                return FakeLocatorChain(indexed[idx])
            return FakeLocatorChain(FakeLocator(count=0, editable=False))
        if ":not([type=\"hidden\"])" in selector:
            return FakeLocatorChain(fallback)
        return FakeLocatorChain(FakeLocator(count=0, editable=False))

    page.locator = MagicMock(side_effect=locator)
    page.url = "https://authenticator.cursor.sh/magic-code?x=1"
    return page


@pytest.mark.asyncio
async def test_collect_otp_digit_locators_skips_hidden_phone_fields() -> None:
    indexed = [FakeLocator(editable=True) for _ in range(6)]
    hidden_phone = FakeLocator(editable=False)
    flow = CursorBrowserFlow(_make_page(indexed=indexed, fallback=hidden_phone), "[test]")

    locators = await flow._collect_otp_digit_locators(min_digits=4)
    assert len(locators) == 6


@pytest.mark.asyncio
async def test_enter_email_code_uses_visible_otp_boxes_only() -> None:
    boxes = [FakeLocator(editable=True) for _ in range(6)]
    flow = CursorBrowserFlow(_make_page(indexed=boxes), "[test]")
    flow.sleep = AsyncMock()

    await flow.enter_email_code("033764")

    assert [box.filled for box in boxes[:6]] == [["0"], ["3"], ["3"], ["7"], ["6"], ["4"]]
