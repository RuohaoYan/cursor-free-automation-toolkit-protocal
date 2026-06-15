from __future__ import annotations

from modules.chatgpt_register import recoverable_auth_error_reason


def test_recoverable_auth_error_reason_detects_route_error() -> None:
    text = (
        '糟糕，出错了！ Route Error (400 Invalid content type: text/html; charset=UTF-8): '
        '"Invalid content type: text/html; charset=UTF-8" 重试'
    )

    assert recoverable_auth_error_reason(text.lower(), text) == "route error"


def test_recoverable_auth_error_reason_ignores_generic_error_without_retry() -> None:
    text = "Something went wrong while loading a decorative widget"

    assert recoverable_auth_error_reason(text.lower(), text) == ""
