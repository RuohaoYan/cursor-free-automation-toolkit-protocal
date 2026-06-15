from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from modules.cursor_register import (
    CURSOR_SMS_CODE_TIME_SKEW_SEC,
    PhoneVerificationRateLimitedError,
    _accept_sms_code,
    _after_request_new_sms_code,
    _mark_sms_since,
    _prepare_sms_baseline,
    _raise_if_phone_rate_limited,
    _snapshot_existing_sms_code,
)
from modules.hero_sms_provider import PhoneCountry, SmsActivation, SmsCodeStatus, parse_sms_timestamp


class _FakeProvider:
    def __init__(self, code: str = "", *, fail: bool = False) -> None:
        self.code = code
        self.fail = fail
        self.calls = 0

    def get_status(self, activation_id: int) -> tuple[bool, str]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("api down")
        if not self.code:
            return False, ""
        return True, self.code


def test_snapshot_existing_sms_code_parses_full_message() -> None:
    provider = _FakeProvider("758764 是您的验证码 Cursor")
    assert _snapshot_existing_sms_code(provider, 1) == "758764"


def test_snapshot_existing_sms_code_returns_empty_when_waiting() -> None:
    provider = _FakeProvider()
    assert _snapshot_existing_sms_code(provider, 1) == ""


def test_prepare_sms_baseline_adds_existing_code(monkeypatch: Any) -> None:
    provider = _FakeProvider("365532")
    activation = SmsActivation(activation_id=99, phone_number="+85212345678")
    used_codes: set[str] = set()
    logs: list[str] = []

    def fake_log(message: str) -> None:
        logs.append(message)

    monkeypatch.setattr("modules.cursor_register.log", fake_log)

    added = asyncio.run(_prepare_sms_baseline(provider, activation, used_codes, "[test]"))

    assert added is True
    assert used_codes == {"365532"}
    assert provider.calls == 1
    assert any("365532" in line for line in logs)


def test_prepare_sms_baseline_skips_duplicate() -> None:
    provider = _FakeProvider("365532")
    activation = SmsActivation(activation_id=99, phone_number="+85212345678")
    used_codes = {"365532"}

    added = asyncio.run(_prepare_sms_baseline(provider, activation, used_codes, "[test]"))

    assert added is True
    assert used_codes == {"365532"}


def test_parse_sms_timestamp_supports_unix_and_iso() -> None:
    assert parse_sms_timestamp(1_700_000_000) is not None
    assert parse_sms_timestamp("2026-02-16T12:36:59+03:00") is not None


def test_accept_sms_code_rejects_old_timestamp() -> None:
    anchor = datetime(2026, 2, 16, 12, 0, 0, tzinfo=timezone.utc)
    old = anchor - timedelta(seconds=CURSOR_SMS_CODE_TIME_SKEW_SEC + 10)
    status = SmsCodeStatus(True, "925888", old)

    accepted = _accept_sms_code(status, set(), anchor, "[test]")

    assert accepted is None


def test_accept_sms_code_accepts_fresh_timestamp() -> None:
    anchor = datetime(2026, 2, 16, 12, 0, 0, tzinfo=timezone.utc)
    fresh = anchor + timedelta(seconds=5)
    status = SmsCodeStatus(True, "925888", fresh)

    accepted = _accept_sms_code(status, set(), anchor, "[test]")

    assert accepted == "925888"


def test_mark_sms_since_stores_anchor(monkeypatch: Any) -> None:
    fixed = datetime(2026, 2, 16, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("modules.cursor_register.now_utc", lambda: fixed)
    sms_selection: dict[str, object] = {}

    since = _mark_sms_since(sms_selection, "[test]", "填写手机号后")

    assert since == fixed
    assert sms_selection["cursor_sms_since"] == fixed


class _FakeSmsFlow:
    def __init__(self, state: str) -> None:
        self.state = state
        self.slept_ms: list[int] = []
        self.page = object()
        self.rate_limited = False

    async def sleep(self, ms: int) -> None:
        self.slept_ms.append(ms)

    async def detect_state(self) -> str:
        return self.state

    async def is_phone_verification_rate_limited(self) -> bool:
        return self.rate_limited


def test_after_request_new_sms_code_refills_when_back_on_phone_page(monkeypatch: Any) -> None:
    provider = _FakeProvider()
    activation = SmsActivation(activation_id=99, phone_number="+85291234567")
    country = PhoneCountry(
        iso_code="HK",
        dial_code="852",
        name="Hong Kong",
        hero_sms_country=14,
    )
    sms_selection: dict[str, object] = {}
    used_codes: set[str] = set()
    flow = _FakeSmsFlow("phone")
    fill_calls: list[tuple[str, str]] = []

    async def fake_fill(page: Any, phone: str, c: PhoneCountry, logger: Any, **kwargs: Any) -> None:
        fill_calls.append((phone, c.iso_code))
        if kwargs.get("before_auto_send"):
            kwargs["before_auto_send"]()

    async def fake_wait(page: Any, logger: Any, timeout: int = 45) -> bool:
        return True

    monkeypatch.setattr("modules.cursor_register.fill_phone_and_wait_sms_page", fake_fill)
    monkeypatch.setattr("modules.cursor_register.wait_for_sms_verification_page", fake_wait)

    asyncio.run(
        _after_request_new_sms_code(
            flow,  # type: ignore[arg-type]
            provider=provider,
            activation=activation,
            country=country,
            sms_selection=sms_selection,
            used_codes=used_codes,
            prefix="[test]",
        )
    )

    assert fill_calls == [("+85291234567", "HK")]
    assert "cursor_sms_since" in sms_selection
    assert flow.slept_ms == [1500]


def test_after_request_new_sms_code_keeps_sms_page_anchor_when_not_phone(monkeypatch: Any) -> None:
    provider = _FakeProvider()
    activation = SmsActivation(activation_id=99, phone_number="+85291234567")
    country = PhoneCountry(
        iso_code="HK",
        dial_code="852",
        name="Hong Kong",
        hero_sms_country=14,
    )
    sms_selection: dict[str, object] = {}
    used_codes: set[str] = set()
    flow = _FakeSmsFlow("sms_code")
    fill_called = False

    async def fake_fill(*args: Any, **kwargs: Any) -> None:
        nonlocal fill_called
        fill_called = True

    monkeypatch.setattr("modules.cursor_register.fill_phone_and_wait_sms_page", fake_fill)
    fixed = datetime(2026, 2, 16, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("modules.cursor_register.now_utc", lambda: fixed)

    asyncio.run(
        _after_request_new_sms_code(
            flow,  # type: ignore[arg-type]
            provider=provider,
            activation=activation,
            country=country,
            sms_selection=sms_selection,
            used_codes=used_codes,
            prefix="[test]",
        )
    )

    assert fill_called is False
    assert sms_selection["cursor_sms_since"] == fixed


def test_raise_if_phone_rate_limited_raises() -> None:
    activation = SmsActivation(activation_id=99, phone_number="+85291234567")
    flow = _FakeSmsFlow("phone")

    async def rate_limited() -> bool:
        return True

    flow.is_phone_verification_rate_limited = rate_limited  # type: ignore[method-assign]

    with pytest.raises(PhoneVerificationRateLimitedError):
        asyncio.run(_raise_if_phone_rate_limited(flow, "[test]", activation))  # type: ignore[arg-type]
