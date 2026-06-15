from __future__ import annotations

import asyncio
import re
import secrets
import string
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .browser import BrowserSession
from .captcha_auto import captcha_strategy_summary, security_challenge_visible
from .chatgpt_register import (
    fill_phone_and_wait_sms_page,
    wait_for_sms_verification_page,
)
from .cursor_browser_flow import CURSOR_HOME_URL, CURSOR_SIGNUP_URL, CursorBrowserFlow
from .fivesim_sms_provider import FiveSimProvider
from .grizzly_sms_provider import GrizzlySMSProvider
from .hero_sms_provider import (
    HeroSMSProvider,
    PhoneCountry,
    SmsActivation,
    SmsCodeStatus,
    resolve_hero_sms_operator_for_purchase,
)
from .mail_provider import MailProvider
from .proxy_pool import ProxyPool
from .storage import MailAccount
from .utils import env_bool, extract_code, load_env, log, now_utc, resolve_path, safe_filename

from . import free_register


CURSOR_OUTPUT_ROOT = "output/cursor注册"
CURSOR_ACCOUNT_FILE = "account.txt"
CURSOR_EMAIL_CODE_INITIAL_WAIT_SEC = 20
CURSOR_EMAIL_CODE_METHOD_STUCK_SEC = 15
CURSOR_EMAIL_CODE_METHOD_CAPTCHA_MAX = 3
CURSOR_EMAIL_CODE_METHOD_CAPTCHA_INTERVAL_SEC = 10
CURSOR_HUMAN_VERIFY_FAIL_PAUSE_SEC = 30
CURSOR_STEP_SETTLE_MS = 800
CURSOR_SMS_CODE_AUTO_ADVANCE_WAIT_SEC = 30
CURSOR_SMS_CODE_VERIFY_WAIT_SEC = 20
CURSOR_SMS_CODE_RESEND_WAIT_SEC = 60
CURSOR_SMS_CODE_TIME_SKEW_SEC = 45
CURSOR_SMS_PHONE_NUMBER_RETRIES = 1
CURSOR_POST_SIGNUP_SKIP_CLICKS = 4


def _is_headless_captcha_failure(exc: BaseException) -> bool:
    return "无头模式安全验证未通过" in str(exc)


def _headed_captcha_fallback_enabled(env: dict[str, str]) -> bool:
    return env_bool(env.get("CURSOR_HEADLESS_CAPTCHA_FALLBACK"), default=True)


async def _switch_browser_to_headed(
    session: BrowserSession,
    *,
    profile_dir: Path,
    browser_cfg: dict[str, Any],
    proxy: str | None,
    incognito: bool,
    account_email: str,
    prefix: str,
) -> tuple[BrowserSession, CursorBrowserFlow]:
    from .proxy_chain_forwarder import reset_chained_forwarders

    reset_chained_forwarders()
    await session.__aexit__(None, None, None)
    new_session = BrowserSession(
        profile_dir=profile_dir,
        headless=False,
        slow_mo=int(browser_cfg.get("slow_mo", 80)),
        timeout_ms=int(browser_cfg.get("timeout_ms", 60000)),
        proxy=proxy,
        incognito=incognito,
        fingerprint_seed=account_email,
    )
    await new_session.__aenter__()
    assert new_session.page is not None
    log(f"{prefix} 已切换为有界面 Chrome（无头 Captcha 回退）")
    return new_session, CursorBrowserFlow(new_session.page, prefix)


class CursorRegisterError(RuntimeError):
    pass


class CursorHumanVerificationError(CursorRegisterError):
    """Cursor blocked registration because human verification failed."""


class PhoneVerificationRateLimitedError(Exception):
    """Cursor phone page rejected number: too many verification requests."""


class EmailVerificationStepEnded(RuntimeError):
    """Page left email OTP step (e.g. advanced to phone/SMS verification)."""


CURSOR_SMS_MAX_PRICE_DEFAULT = 0.1


def _resolve_cursor_sms_resend_wait_sec(sms_selection: dict[str, object] | None = None) -> float:
    if sms_selection is not None:
        raw = sms_selection.get("cursor_sms_resend_wait_sec")
        if raw is not None and str(raw).strip():
            return float(raw)
    env = load_env(".env")
    return float(env.get("CURSOR_SMS_CODE_RESEND_WAIT_SEC") or CURSOR_SMS_CODE_RESEND_WAIT_SEC)


def _resolve_cursor_sms_max_price(sms_selection: dict[str, object] | None = None) -> float:
    if sms_selection is not None:
        raw = sms_selection.get("max_price")
        if raw is not None and str(raw).strip():
            return float(raw)
    env = load_env(".env")
    return float(env.get("CURSOR_SMS_MAX_PRICE") or CURSOR_SMS_MAX_PRICE_DEFAULT)


def _assert_hero_sms_price_allowed(
    provider: HeroSMSProvider,
    service: str,
    country: PhoneCountry,
    max_price: float,
    prefix: str,
) -> None:
    quoted = provider.quote_service_country(service, country.hero_sms_country)
    price = quoted.get("price") if quoted else None
    stock = quoted.get("count") if quoted else None
    if price is None:
        log(f"{prefix} [SMS] 未获取到报价，继续尝试取号（maxPrice={max_price}）")
        return
    log(f"{prefix} [SMS] 当前报价: ${price}, 库存={stock}, 上限=${max_price}")
    if price > max_price:
        raise CursorRegisterError(f"HeroSMS 报价 ${price} 超过上限 ${max_price}，跳过购买")


def cursor_output_paths(root: str | Path = CURSOR_OUTPUT_ROOT) -> tuple[Path, Path]:
    base = resolve_path(root)
    base.mkdir(parents=True, exist_ok=True)
    account_file = base / CURSOR_ACCOUNT_FILE
    if not account_file.exists():
        account_file.write_text("", encoding="utf-8")
    return base, account_file


def generate_cursor_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "Cr" + "".join(secrets.choice(alphabet) for _ in range(14)) + "9!"


def split_full_name(full_name: str) -> tuple[str, str]:
    parts = [p for p in full_name.strip().split() if p]
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    if parts:
        return parts[0], secrets.choice(["Smith", "Johnson", "Brown", "Davis", "Wilson"])
    return secrets.choice(["Alex", "Jordan", "Taylor"]), secrets.choice(["Smith", "Lee", "Kim"])


def append_cursor_account(
    email: str,
    email_password: str,
    cursor_password: str,
    first_name: str,
    last_name: str,
    *,
    output_root: str | Path = CURSOR_OUTPUT_ROOT,
) -> Path:
    _, account_file = cursor_output_paths(output_root)
    line = f"{email}----{email_password}----{cursor_password}----{first_name}----{last_name}"
    with account_file.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return account_file


async def _snapshot_existing_email_codes(
    mail_provider: MailProvider,
    account: MailAccount,
    bad_codes: set[str],
    *,
    before: datetime | None = None,
) -> set[str]:
    if before is not None:
        existing = await mail_provider.snapshot_codes_before(account, before)
    else:
        existing = await mail_provider.snapshot_existing_codes(account)
    if not existing:
        return set()
    bad_codes.update(existing)
    return existing


async def _anchor_email_verification_request(
    mail_provider: MailProvider,
    account: MailAccount,
    bad_codes: set[str],
    prefix: str,
    *,
    reason: str,
) -> datetime:
    """在触发发码时锚定时间，仅排除锚点之前收到的旧验证码。"""
    since = now_utc()
    log(f"{prefix} 邮箱验证码锚点 ({reason}): {since.isoformat()}")
    existing = await _snapshot_existing_email_codes(
        mail_provider, account, bad_codes, before=since
    )
    if existing:
        log(
            f"{prefix} 排除锚点前旧验证码 {len(existing)} 个: "
            f"{', '.join(sorted(existing))}"
        )
    else:
        log(f"{prefix} 锚点前无更早的 Cursor 验证码，本封邮件到达后可直接使用")
    return since


async def _wait_email_code(
    flow: CursorBrowserFlow,
    account: MailAccount,
    mail_provider: MailProvider,
    since: Any,
    bad_codes: set[str],
    prefix: str,
    *,
    wait_before_resend_sec: float | None = CURSOR_EMAIL_CODE_INITIAL_WAIT_SEC,
) -> tuple[str, Any]:
    async def _poll_once() -> str | None:
        if not await flow.is_still_email_verification_step():
            raise EmailVerificationStepEnded("page advanced to phone/SMS verification")
        return await mail_provider.fetch_code(account, since, bad_codes)

    poll_interval = float(getattr(mail_provider, "poll_interval_sec", 5) or 5)

    if wait_before_resend_sec is not None and wait_before_resend_sec > 0:
        code = await _poll_once()
        if code:
            return code, since
        wait_sec = int(wait_before_resend_sec)
        log(f"{prefix} 进入验证码页，先等待 {wait_sec} 秒接收新邮件...")
        deadline = time.monotonic() + wait_sec
        while time.monotonic() < deadline:
            code = await _poll_once()
            if code:
                return code, since
            await asyncio.sleep(poll_interval)
        log(f"{prefix} {wait_sec} 秒内未收到新邮件，点击「立即重发」...")
        if await flow.click_resend_code():
            since = await _anchor_email_verification_request(
                mail_provider,
                account,
                bad_codes,
                prefix,
                reason="35秒内无新邮件重发",
            )
            log(f"{prefix} 已点击「立即重发」，继续等待新验证码")
            await flow.sleep(1500)
        else:
            log(f"{prefix} 未找到「立即重发」按钮，继续等待新邮件")

    deadline = time.monotonic() + mail_provider.timeout_sec
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            code = await _poll_once()
            if code:
                return code, since
        except EmailVerificationStepEnded:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        await asyncio.sleep(poll_interval)
    raise TimeoutError(f"验证码等待超时: {last_error or '没有新验证码'}")


def _normalize_sms_code(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    parsed = extract_code(text)
    if parsed:
        return parsed
    digits = re.sub(r"\D+", "", text)
    if 4 <= len(digits) <= 8:
        return digits
    return ""


def _sms_since_floor(since: datetime) -> datetime:
    return since - timedelta(seconds=CURSOR_SMS_CODE_TIME_SKEW_SEC)


def _mark_sms_since(sms_selection: dict[str, object], prefix: str, reason: str) -> datetime:
    since = now_utc()
    sms_selection["cursor_sms_since"] = since
    log(f"{prefix} [SMS] 短信锚点时间 ({reason}): {since.isoformat()}")
    return since


def _fetch_sms_status(provider, activation_id: int) -> SmsCodeStatus:
    if hasattr(provider, "get_status_detail"):
        return provider.get_status_detail(activation_id)
    received, code = provider.get_status(activation_id)
    return SmsCodeStatus(bool(received), str(code or "").strip(), None)


def _accept_sms_code(
    status: SmsCodeStatus,
    used_codes: set[str],
    sms_since: datetime | None,
    prefix: str,
) -> str | None:
    normalized = _normalize_sms_code(status.code)
    if not status.received or not normalized:
        return None
    if normalized in used_codes:
        log(f"{prefix} [SMS] ignored reused SMS code: {normalized}")
        return None
    if sms_since and status.received_at:
        floor = _sms_since_floor(sms_since)
        if status.received_at < floor:
            log(
                f"{prefix} [SMS] ignored old SMS code {normalized} "
                f"(received {status.received_at.isoformat()} < {floor.isoformat()})"
            )
            return None
    if sms_since and status.received_at:
        log(
            f"{prefix} [SMS] accepted SMS code {normalized} "
            f"(received {status.received_at.isoformat()} >= anchor {sms_since.isoformat()})"
        )
    return normalized


async def _snapshot_sms_code_to_used(
    provider,
    activation_id: int,
    used_codes: set[str],
    prefix: str,
) -> str:
    status = await asyncio.to_thread(_fetch_sms_status, provider, activation_id)
    existing = _normalize_sms_code(status.code)
    if not status.received or not existing:
        return ""
    if existing in used_codes:
        return existing
    used_codes.add(existing)
    if status.received_at:
        log(
            f"{prefix} [SMS] 标记历史短信验证码为旧码 "
            f"({status.received_at.isoformat()}): {existing}"
        )
    else:
        log(f"{prefix} [SMS] 标记历史短信验证码为旧码: {existing}")
    return existing


async def _after_request_new_sms_code(
    flow: CursorBrowserFlow,
    *,
    provider,
    activation: SmsActivation,
    country: PhoneCountry,
    sms_selection: dict[str, object],
    used_codes: set[str],
    prefix: str,
) -> None:
    """「请求新的验证码」后可能回到手机号页，需重新填号提交才会再次发短信。"""
    await _snapshot_sms_code_to_used(provider, activation.activation_id, used_codes, prefix)
    await flow.sleep(1500)
    state = await flow.detect_state()
    if state == "phone":
        log(
            f"{prefix} [SMS] 请求新验证码后回到手机号页，"
            f"重新填号并提交 {activation.phone_number}"
        )
        await fill_phone_and_wait_sms_page(
            flow.page,
            activation.phone_number,
            country,
            log,
            before_auto_send=lambda: _mark_sms_since(
                sms_selection,
                prefix,
                "请求新码后重新提交",
            ),
        )
        await _wait_sms_verification_or_raise_rate_limited(flow, prefix, activation)
        return
    _mark_sms_since(sms_selection, prefix, "请求新的验证码")
    log(f"{prefix} [SMS] 仍在短信验证码页，已重置锚点")


async def _raise_if_phone_rate_limited(
    flow: CursorBrowserFlow,
    prefix: str,
    activation: SmsActivation,
) -> None:
    if await flow.is_phone_verification_rate_limited():
        log(
            f"{prefix} [SMS] 页面提示此手机号验证请求过多，将取消并换新号: "
            f"{activation.phone_number}"
        )
        raise PhoneVerificationRateLimitedError(activation.phone_number)


async def _wait_sms_verification_or_raise_rate_limited(
    flow: CursorBrowserFlow,
    prefix: str,
    activation: SmsActivation,
) -> str:
    result = await wait_for_sms_verification_page(flow.page, log)
    if result == "rate_limited":
        log(
            f"{prefix} [SMS] 等待验证码页时出现「验证请求过多」，"
            f"将取消并换新号: {activation.phone_number}"
        )
        raise PhoneVerificationRateLimitedError(activation.phone_number)
    if result != "sms":
        log(f"{prefix} [SMS] 未明确检测到验证码页 ({result})，继续轮询")
    return result


def _snapshot_existing_sms_code(provider, activation_id: int) -> str:
    status = _fetch_sms_status(provider, activation_id)
    if not status.received:
        return ""
    return _normalize_sms_code(status.code)


async def _prepare_sms_baseline(
    provider,
    activation: SmsActivation,
    used_codes: set[str],
    prefix: str,
) -> bool:
    existing = await asyncio.to_thread(_snapshot_existing_sms_code, provider, activation.activation_id)
    if not existing:
        log(f"{prefix} [SMS] 取号后暂无历史短信验证码")
        return False
    if existing in used_codes:
        log(f"{prefix} [SMS] 取号快照历史验证码 {existing}（已在排除列表）")
        return True
    used_codes.add(existing)
    log(
        f"{prefix} [SMS] 取号后标记 1 个历史短信验证码为旧码，"
        f"仅接受后续新短信: {existing}"
    )
    return True


async def _purchase_cursor_phone_activation(
    provider,
    provider_name: str,
    provider_label: str,
    *,
    service: str,
    country: PhoneCountry,
    country_arg: object,
    operator_value: str,
    max_price: float,
    sms_selection: dict[str, object],
    used_codes: set[str],
    max_reuse: int,
    prefix: str,
) -> SmsActivation:
    log(
        f"{prefix} [SMS] {provider_label} request phone: service={service}, "
        f"country={country.name}(+{country.dial_code}), maxPrice={max_price}"
    )
    if provider_name == "herosms" and isinstance(provider, HeroSMSProvider):
        await asyncio.to_thread(
            _assert_hero_sms_price_allowed,
            provider,
            service,
            country,
            max_price,
            prefix,
        )
        if not operator_value:
            operator_value = await asyncio.to_thread(
                resolve_hero_sms_operator_for_purchase,
                provider,
                service,
                country.hero_sms_country,
                max_price,
                prefix,
            )
    activation = await asyncio.to_thread(
        provider.get_number,
        service,
        country_arg,
        operator=operator_value,
        max_price=max_price if provider_name == "herosms" else None,
    )
    sms_selection["cursor_phone_activation"] = activation
    sms_selection["last_activation"] = activation
    sms_selection["cursor_phone_use_count"] = 0
    sms_selection["cursor_phone_purchased_at"] = time.monotonic()
    sms_selection["cursor_phone_has_code"] = False
    used_codes.clear()
    log(f"{prefix} [SMS] got phone {activation.phone_number} (reuse 0/{max_reuse})")
    await asyncio.to_thread(provider.mark_ready, activation.activation_id)
    return activation


async def _handle_phone_sms(
    flow: CursorBrowserFlow,
    sms_selection: dict[str, object],
    prefix: str,
    *,
    fill_phone: bool = True,
) -> None:
    provider_name = str(sms_selection.get("provider") or "herosms").lower()
    if provider_name in {"fivesim", "5sim"}:
        provider_name = "fivesim"
    provider_label = str(sms_selection.get("provider_label") or "HeroSMS")
    api_key = str(sms_selection.get("api_key") or "").strip()
    service = str(sms_selection.get("service") or "ot").strip() or "ot"
    country = sms_selection.get("country")
    operator = sms_selection.get("operator")
    if not api_key or not isinstance(country, PhoneCountry):
        raise CursorRegisterError("HeroSMS SMS config is incomplete")
    operator_value = str(getattr(operator, "operator", "") or "").strip()
    if provider_name == "fivesim" and not operator_value:
        operator_value = "any"
    poll_interval = float(sms_selection.get("poll_interval") or 5.0)
    max_attempts = int(sms_selection.get("max_attempts") or 60)
    max_price = _resolve_cursor_sms_max_price(sms_selection)

    if provider_name == "grizzly":
        provider = GrizzlySMSProvider(api_key)
    elif provider_name == "fivesim":
        provider = FiveSimProvider(api_key)
    else:
        provider = HeroSMSProvider(api_key)

    country_arg: object = country if provider_name == "fivesim" else country.hero_sms_country
    used_codes = sms_selection.setdefault("cursor_phone_used_codes", set())
    if not isinstance(used_codes, set):
        used_codes = set()
        sms_selection["cursor_phone_used_codes"] = used_codes
    max_reuse = int(sms_selection.get("cursor_phone_max_reuse") or 3)
    first_code_timeout = float(sms_selection.get("cursor_phone_first_code_timeout_sec") or 120)
    lifetime_sec = float(sms_selection.get("cursor_phone_lifetime_sec") or 1200)
    resend_wait_sec = _resolve_cursor_sms_resend_wait_sec(sms_selection)
    use_count = int(sms_selection.get("cursor_phone_use_count") or 0)
    phone_rounds = int(sms_selection.get("cursor_phone_number_retries") or CURSOR_SMS_PHONE_NUMBER_RETRIES) + 1
    code_received = False
    activation: SmsActivation | None = None
    try:
        force_new_phone = False
        for phone_round in range(1, phone_rounds + 1):
            if force_new_phone:
                previous = sms_selection.get("cursor_phone_activation")
                if isinstance(previous, SmsActivation):
                    await asyncio.to_thread(provider.cancel, previous.activation_id)
                _clear_reused_phone_state(sms_selection)
                activation = None
                log(f"{prefix} [SMS] 申请新手机号 ({phone_round}/{phone_rounds})...")

            purchased_at = float(sms_selection.get("cursor_phone_purchased_at") or 0)
            expired = bool(purchased_at and time.monotonic() - purchased_at >= lifetime_sec)
            activation = sms_selection.get("cursor_phone_activation")
            if not isinstance(activation, SmsActivation):
                activation = None
            if expired and activation is not None:
                await asyncio.to_thread(provider.complete, activation.activation_id)
                _clear_reused_phone_state(sms_selection)
                activation = None
                log(f"{prefix} [SMS] phone lifetime exceeded {int(lifetime_sec)}s, completed activation")

            if force_new_phone or activation is None or use_count >= max_reuse:
                activation = await _purchase_cursor_phone_activation(
                    provider,
                    provider_name,
                    provider_label,
                    service=service,
                    country=country,
                    country_arg=country_arg,
                    operator_value=operator_value,
                    max_price=max_price,
                    sms_selection=sms_selection,
                    used_codes=used_codes,
                    max_reuse=max_reuse,
                    prefix=prefix,
                )
            else:
                log(f"{prefix} [SMS] reuse phone {activation.phone_number} ({use_count}/{max_reuse})")

            await _prepare_sms_baseline(provider, activation, used_codes, prefix)

            try:
                round_fill_phone = fill_phone or force_new_phone or phone_round > 1
                if round_fill_phone:
                    log(f"{prefix} [SMS] 返回手机号页填写 {activation.phone_number}")
                    await fill_phone_and_wait_sms_page(
                        flow.page,
                        activation.phone_number,
                        country,
                        log,
                        before_auto_send=lambda: _mark_sms_since(
                            sms_selection,
                            prefix,
                            "点击提交(自动跳转发码)",
                        ),
                    )
                    await _raise_if_phone_rate_limited(flow, prefix, activation)
                    await _wait_sms_verification_or_raise_rate_limited(flow, prefix, activation)

                sms_since = sms_selection.get("cursor_sms_since")
                if not isinstance(sms_since, datetime):
                    if round_fill_phone:
                        _mark_sms_since(sms_selection, prefix, "开始轮询前")
                    else:
                        log(
                            f"{prefix} [SMS] 已在验证码页，短信应已自动发送，"
                            f"仅按 used_codes 过滤(无提交锚点时不做时间过滤)"
                        )

                sms_verified = False
                for sms_attempt in range(1, 4):
                    raw_code = await _poll_sms_code_with_resend(
                        flow,
                        provider,
                        activation,
                        country,
                        used_codes,
                        sms_selection=sms_selection,
                        prefix=prefix,
                        poll_interval=poll_interval,
                        max_attempts=max_attempts,
                        purchased_at=float(sms_selection.get("cursor_phone_purchased_at") or time.monotonic()),
                        has_any_code=bool(sms_selection.get("cursor_phone_has_code")),
                        first_code_timeout=first_code_timeout,
                        lifetime_sec=lifetime_sec,
                        resend_wait_sec=resend_wait_sec,
                    )
                    code = _normalize_sms_code(raw_code)
                    if not code:
                        raise RuntimeError(f"无法从短信提取验证码: {raw_code}")
                    code_received = True
                    sms_selection["cursor_phone_has_code"] = True
                    log(f"{prefix} [SMS] received verification code: {code}")
                    await flow.enter_sms_code(code, log)
                    log(
                        f"{prefix} [SMS] 短信验证码已填入，"
                        f"等待页面校验 {CURSOR_SMS_CODE_VERIFY_WAIT_SEC} 秒..."
                    )
                    result = await flow.wait_sms_code_result(timeout_sec=CURSOR_SMS_CODE_VERIFY_WAIT_SEC)
                    log(f"{prefix} [SMS] 页面校验结果: {result}")
                    if result == "rejected":
                        used_codes.add(code)
                        log(f"{prefix} [SMS] 短信验证码 {code} 无效，请求新验证码后重试 ({sms_attempt}/3)")
                        if sms_attempt >= 3:
                            raise RuntimeError("短信验证码连续无效，已达最大重试次数")
                        if not await flow.click_request_new_sms_code():
                            log(f"{prefix} [SMS] 未找到「请求新的验证码」按钮，继续轮询")
                        else:
                            await _after_request_new_sms_code(
                                flow,
                                provider=provider,
                                activation=activation,
                                country=country,
                                sms_selection=sms_selection,
                                used_codes=used_codes,
                                prefix=prefix,
                            )
                        continue
                    used_codes.add(code)
                    sms_verified = True
                    log(f"{prefix} [SMS] 短信验证码已通过校验")
                    break
                if not sms_verified:
                    raise RuntimeError("短信验证码提交失败")
            except PhoneVerificationRateLimitedError:
                await asyncio.to_thread(provider.cancel, activation.activation_id)
                _clear_reused_phone_state(sms_selection)
                if phone_round >= phone_rounds:
                    raise CursorRegisterError(
                        "手机号验证请求过多，已达换号上限"
                    )
                log(
                    f"{prefix} [SMS] 验证请求过多，取消旧号并换新号 "
                    f"({phone_round}/{phone_rounds})"
                )
                force_new_phone = True
                code_received = False
                continue
            except TimeoutError as exc:
                if phone_round >= phone_rounds:
                    raise CursorRegisterError(f"phone SMS failed: {exc}") from exc
                log(
                    f"{prefix} [SMS] 号码 {activation.phone_number} 未收到验证码，"
                    f"取消并换新号 ({phone_round}/{phone_rounds})"
                )
                force_new_phone = True
                code_received = False
                continue

            log(
                f"{prefix} [SMS] 等待 {CURSOR_SMS_CODE_AUTO_ADVANCE_WAIT_SEC} 秒页面自动跳转..."
            )
            await flow.sleep(CURSOR_SMS_CODE_AUTO_ADVANCE_WAIT_SEC * 1000)
            skipped = await flow.dismiss_post_signup_prompts(CURSOR_POST_SIGNUP_SKIP_CLICKS)
            log(f"{prefix} 注册引导页已跳过 {skipped}/{CURSOR_POST_SIGNUP_SKIP_CLICKS} 次 Later/之后再说")
            log(f"{prefix} [SMS] 确认验证码已使用 (HeroSMS complete)")
            await asyncio.to_thread(provider.complete, activation.activation_id)
            sms_selection["cursor_phone_use_count"] = use_count + 1
            return
    except CursorRegisterError:
        raise
    except Exception as exc:
        if activation is not None and not code_received:
            await asyncio.to_thread(provider.cancel, activation.activation_id)
            _clear_reused_phone_state(sms_selection)
        raise CursorRegisterError(f"phone SMS failed: {exc}") from exc


async def _poll_sms_code_with_resend(
    flow: CursorBrowserFlow,
    provider,
    activation: SmsActivation,
    country: PhoneCountry,
    used_codes: set[str],
    *,
    sms_selection: dict[str, object],
    prefix: str,
    poll_interval: float,
    max_attempts: int,
    purchased_at: float,
    has_any_code: bool,
    first_code_timeout: float,
    lifetime_sec: float,
    resend_wait_sec: float = CURSOR_SMS_CODE_RESEND_WAIT_SEC,
) -> str:
    poll_started = time.monotonic()
    resend_clicked = False
    resend_wait_sec = max(10.0, float(resend_wait_sec))
    sms_since = sms_selection.get("cursor_sms_since")
    if not isinstance(sms_since, datetime):
        sms_since = _mark_sms_since(sms_selection, prefix, "轮询开始")
    activation_id = activation.activation_id
    for attempt in range(1, max_attempts + 1):
        log(f"[SMS] polling SMS code: activation={activation_id} ({attempt}/{max_attempts})")
        elapsed = time.monotonic() - purchased_at
        if not has_any_code and elapsed >= first_code_timeout:
            raise TimeoutError(f"no SMS code within first {int(first_code_timeout)}s")
        if elapsed >= lifetime_sec:
            raise TimeoutError(f"phone activation lifetime exceeded {int(lifetime_sec)}s")
        status = await asyncio.to_thread(_fetch_sms_status, provider, activation_id)
        sms_since = sms_selection.get("cursor_sms_since")
        if not isinstance(sms_since, datetime):
            sms_since = None
        normalized = _accept_sms_code(status, used_codes, sms_since, prefix)
        if normalized:
            return normalized
        wait_elapsed = time.monotonic() - poll_started
        if (
            not resend_clicked
            and not has_any_code
            and wait_elapsed >= resend_wait_sec
        ):
            log(
                f"{prefix} [SMS] {int(resend_wait_sec)}s 内未收到验证码，"
                f"点击「请求新的验证码」"
            )
            if await flow.click_request_new_sms_code():
                resend_clicked = True
                await _after_request_new_sms_code(
                    flow,
                    provider=provider,
                    activation=activation,
                    country=country,
                    sms_selection=sms_selection,
                    used_codes=used_codes,
                    prefix=prefix,
                )
                log(f"{prefix} [SMS] 已点击请求新的验证码，继续等待")
            else:
                log(f"{prefix} [SMS] 未找到「请求新的验证码」按钮，继续轮询")
        await asyncio.sleep(max(1.0, poll_interval))
    raise TimeoutError("SMS verification code timed out")


def _poll_for_unused_sms_code(
    provider,
    activation_id: int,
    used_codes: set[str],
    interval: float,
    max_attempts: int,
    purchased_at: float,
    has_any_code: bool,
    first_code_timeout: float,
    lifetime_sec: float,
) -> str:
    for attempt in range(1, max_attempts + 1):
        log(f"[SMS] polling SMS code: activation={activation_id} ({attempt}/{max_attempts})")
        elapsed = time.monotonic() - purchased_at
        if not has_any_code and elapsed >= first_code_timeout:
            raise TimeoutError(f"no SMS code within first {int(first_code_timeout)}s")
        if elapsed >= lifetime_sec:
            raise TimeoutError(f"phone activation lifetime exceeded {int(lifetime_sec)}s")
        received, code = provider.get_status(activation_id)
        status = _fetch_sms_status(provider, activation_id)
        sms_since = None
        normalized = _accept_sms_code(status, used_codes, sms_since, "[SMS]")
        if normalized:
            return normalized
        time.sleep(max(1.0, interval))
    raise TimeoutError("SMS verification code timed out")


def _clear_reused_phone_state(sms_selection: dict[str, object]) -> None:
    sms_selection.pop("cursor_phone_activation", None)
    sms_selection.pop("last_activation", None)
    sms_selection.pop("cursor_phone_purchased_at", None)
    sms_selection.pop("cursor_sms_since", None)
    sms_selection["cursor_phone_has_code"] = False
    sms_selection["cursor_phone_use_count"] = 0
    used_codes = sms_selection.get("cursor_phone_used_codes")
    if isinstance(used_codes, set):
        used_codes.clear()


async def _mark_reused_phone_success(sms_selection: dict[str, object] | None, prefix: str) -> None:
    if not sms_selection:
        return
    activation = sms_selection.get("cursor_phone_activation")
    if not isinstance(activation, SmsActivation):
        return
    max_reuse = int(sms_selection.get("cursor_phone_max_reuse") or 3)
    use_count = int(sms_selection.get("cursor_phone_use_count") or 0) + 1
    sms_selection["cursor_phone_use_count"] = use_count
    log(f"{prefix} [SMS] phone success usage {use_count}/{max_reuse}: {activation.phone_number}")
    if use_count < max_reuse:
        return
    provider_name = str(sms_selection.get("provider") or "herosms").lower()
    api_key = str(sms_selection.get("api_key") or "").strip()
    if provider_name == "grizzly":
        provider = GrizzlySMSProvider(api_key)
    elif provider_name in {"fivesim", "5sim"}:
        provider = FiveSimProvider(api_key)
    else:
        provider = HeroSMSProvider(api_key)
    await asyncio.to_thread(provider.complete, activation.activation_id)
    _clear_reused_phone_state(sms_selection)
    log(f"{prefix} [SMS] phone reached success limit {max_reuse}/{max_reuse}, completed activation")


async def _complete_reused_phone_activation(sms_selection: dict[str, object] | None) -> None:
    if not sms_selection:
        return
    activation = sms_selection.get("cursor_phone_activation")
    if not isinstance(activation, SmsActivation):
        return
    provider_name = str(sms_selection.get("provider") or "herosms").lower()
    api_key = str(sms_selection.get("api_key") or "").strip()
    if not api_key:
        return
    if provider_name == "grizzly":
        provider = GrizzlySMSProvider(api_key)
    elif provider_name in {"fivesim", "5sim"}:
        provider = FiveSimProvider(api_key)
    else:
        provider = HeroSMSProvider(api_key)
    try:
        await asyncio.to_thread(provider.complete, activation.activation_id)
        log(f"[SMS] completed reused phone activation: {activation.activation_id}")
    finally:
        _clear_reused_phone_state(sms_selection)


async def run_cursor_register_once(
    cfg: dict[str, Any],
    *,
    sms_selection: dict[str, object] | None,
    worker_id: int = 1,
    proxy: str | None = None,
    mail_source: free_register.FreeMailSource | None = None,
    register_mode: str = "browser",
) -> bool:
    sms_provider_ready = bool(
        sms_selection and str((sms_selection or {}).get("api_key") or "").strip()
    )
    if not sms_provider_ready:
        raise CursorRegisterError("[SMS] 缺少 HERO_SMS_API_KEY，已停止 Cursor 注册")
    sms_lock = sms_selection.setdefault("cursor_phone_lock", asyncio.Lock())
    if not hasattr(sms_lock, "__aenter__"):
        sms_lock = asyncio.Lock()
        sms_selection["cursor_phone_lock"] = sms_lock

    env = load_env(".env")
    mail_source = mail_source or await free_register.create_free_mail_account(cfg, env, worker_id=worker_id, flow="CURSOR")
    account = mail_source.account
    prefix = f"[cursor-{worker_id:02d}][{account.email}]"

    age_min = int(cfg.get("register_profile", {}).get("age_min", 21))
    age_max = int(cfg.get("register_profile", {}).get("age_max", 45))
    full_name, _age = free_register.generate_free_profile(age_min, age_max)
    first_name, last_name = split_full_name(full_name)
    password = account.password or generate_cursor_password()

    mail_cfg = free_register.resolve_free_mail_config(
        cfg,
        free_register.resolve_flow_mail_source(cfg, env, "CURSOR"),
    )
    mail_provider = MailProvider(
        source=mail_cfg["source"],
        timeout_sec=int(mail_cfg.get("code_timeout_sec", 150)),
        poll_interval_sec=int(mail_cfg.get("poll_interval_sec", 5)),
        log_prefix=prefix,
        verification_sender="cursor",
    )

    browser_cfg = cfg.get("browser", {})
    signup_url = str(cfg.get("cursor", {}).get("signup_url") or CURSOR_HOME_URL)
    profile_dir = resolve_path("profiles/cursor") / safe_filename(account.email)
    incognito = env_bool(
        env.get("CURSOR_BROWSER_INCOGNITO") or env.get("BROWSER_INCOGNITO"),
        default=bool(browser_cfg.get("incognito", False)),
    )
    headless = env_bool(
        env.get("CURSOR_BROWSER_HEADLESS") or env.get("BROWSER_HEADLESS"),
        default=bool(browser_cfg.get("headless", False)),
    )
    session = BrowserSession(
        profile_dir=profile_dir,
        headless=headless,
        slow_mo=int(browser_cfg.get("slow_mo", 80)),
        timeout_ms=int(browser_cfg.get("timeout_ms", 60000)),
        proxy=proxy,
        incognito=incognito,
        fingerprint_seed=account.email,
    )

    since = now_utc()
    bad_codes: set[str] = set()
    email_code_anchored = False
    last_state = ""
    unknown_count = 0
    email_code_method_entered_at: float | None = None
    email_code_method_clicked = False
    email_code_method_captcha_attempts = 0
    email_code_method_last_captcha_at = 0.0
    deadline = time.monotonic() + 420
    proto_session = None
    use_protocol = (register_mode or "browser").lower() == "protocol"
    headed_fallback_used = False

    async def maybe_switch_to_headed_for_captcha(exc: BaseException) -> bool:
        nonlocal session, flow, proto_session, headless, headed_fallback_used
        if (
            headed_fallback_used
            or not headless
            or not _is_headless_captcha_failure(exc)
            or not _headed_captcha_fallback_enabled(env)
        ):
            return False
        headed_fallback_used = True
        headless = False
        proto_session = None
        session, flow = await _switch_browser_to_headed(
            session,
            profile_dir=profile_dir,
            browser_cfg=browser_cfg,
            proxy=proxy,
            incognito=incognito,
            account_email=account.email,
            prefix=prefix,
        )
        await flow.navigate_to_signup(signup_url)
        return True

    try:
        await session.__aenter__()
        assert session.page is not None
        flow = CursorBrowserFlow(session.page, prefix)
        mode_label = "协议+浏览器" if use_protocol else "浏览器"
        log(f"{prefix} 开始 Cursor 自动注册（{mode_label}: {'无痕' if incognito else '持久 profile'}）")
        try:
            await flow.navigate_to_signup(signup_url)
        except RuntimeError as exc:
            if not await maybe_switch_to_headed_for_captcha(exc):
                raise

        while time.monotonic() < deadline:
            state = await flow.detect_state()
            if state != last_state:
                log(f"{prefix} 页面状态: {state} ({flow.page.url})")
                if state != "email_code_method":
                    email_code_method_entered_at = None
                    email_code_method_clicked = False
                    email_code_method_captcha_attempts = 0
                    email_code_method_last_captcha_at = 0.0
                last_state = state

            if state == "success" or await flow.is_registration_complete():
                break

            if state == "email_unavailable":
                raise CursorRegisterError("邮箱已被注册")

            if state == "security_challenge":
                if await flow._is_password_step():
                    continue
                if await flow._is_phone_verification_page():
                    continue
                if await flow._is_auth_email_step() or await flow._is_email_code_method_page():
                    continue
                if await flow._needs_name_email_fill() and await flow._signup_form_ready():
                    continue
                try:
                    await flow.wait_for_cloudflare(45_000)
                except RuntimeError as exc:
                    if not await maybe_switch_to_headed_for_captcha(exc):
                        raise
                continue

            if state == "home":
                unknown_count = 0
                if not await flow.click_home_sign_in():
                    await flow.sleep(800)
                continue

            if state == "auth_email":
                unknown_count = 0
                if use_protocol:
                    from .cursor_protocol_register import bootstrap_from_browser, try_protocol_submit_signup

                    try:
                        if proto_session is None:
                            proto_session, _ = await bootstrap_from_browser(session.page, prefix)
                        if await try_protocol_submit_signup(
                            proto_session,
                            email=account.email,
                            first_name=first_name,
                            last_name=last_name,
                            password=password,
                            prefix=prefix,
                        ):
                            await flow.sleep(CURSOR_STEP_SETTLE_MS)
                            continue
                    except Exception as exc:
                        log(f"{prefix} [Protocol] HTTP 提交失败，回退 DOM: {exc.__class__.__name__}")
                try:
                    await flow.fill_auth_email(account.email)
                except RuntimeError as exc:
                    if "继续按钮" in str(exc):
                        log(f"{prefix} 欢迎页提交未成功，等待页面跳转后重试")
                        await flow.sleep(1500)
                        continue
                    raise
                await flow.sleep(CURSOR_STEP_SETTLE_MS)
                continue

            if state == "email_code_method":
                unknown_count = 0
                if email_code_method_entered_at is None:
                    email_code_method_entered_at = time.monotonic()
                stuck_sec = time.monotonic() - email_code_method_entered_at
                if stuck_sec >= CURSOR_EMAIL_CODE_METHOD_STUCK_SEC:
                    if await flow.is_human_verification_blocked():
                        raise CursorHumanVerificationError(
                            f"无法验证用户为真人，{int(stuck_sec)}s 页面未跳转，本邮箱注册失败"
                        )
                    visible, reason = await security_challenge_visible(flow.page)
                    now = time.monotonic()
                    can_retry_captcha = (
                        visible
                        and email_code_method_captcha_attempts < CURSOR_EMAIL_CODE_METHOD_CAPTCHA_MAX
                        and (
                            email_code_method_captcha_attempts == 0
                            or now - email_code_method_last_captcha_at
                            >= CURSOR_EMAIL_CODE_METHOD_CAPTCHA_INTERVAL_SEC
                        )
                    )
                    if can_retry_captcha:
                        email_code_method_captcha_attempts += 1
                        email_code_method_last_captcha_at = now
                        log(
                            f"{prefix} 邮箱验证码方式页 {int(stuck_sec)}s 未跳转，"
                            f"第 {email_code_method_captcha_attempts}/"
                            f"{CURSOR_EMAIL_CODE_METHOD_CAPTCHA_MAX} 次 Captcha"
                            f"({reason})，策略={captcha_strategy_summary()}"
                        )
                        await flow.wait_for_cloudflare(30_000)
                if not email_code_method_clicked:
                    if await flow.click_email_code_method():
                        since = await _anchor_email_verification_request(
                            mail_provider,
                            account,
                            bad_codes,
                            prefix,
                            reason="选择邮箱验证码",
                        )
                        email_code_method_clicked = True
                        email_code_anchored = True
                    else:
                        log(f"{prefix} 等待邮箱验证码发送界面...")
                await flow.sleep(800)
                continue

            if state == "auth_welcome":
                unknown_count = 0
                await flow.fill_auth_email(account.email)
                await flow.sleep(CURSOR_STEP_SETTLE_MS)
                continue

            if state == "name_email":
                unknown_count = 0
                await flow.fill_name_email(first_name, last_name, account.email)
                await flow.sleep(CURSOR_STEP_SETTLE_MS)
                continue

            if state == "password":
                unknown_count = 0
                await flow.fill_password(password)
                await flow.sleep(CURSOR_STEP_SETTLE_MS)
                continue

            if state in {"email_code", "email_code_pending"}:
                unknown_count = 0
                if state == "email_code_pending":
                    if await flow.click_email_code_method():
                        since = await _anchor_email_verification_request(
                            mail_provider,
                            account,
                            bad_codes,
                            prefix,
                            reason="选择邮箱验证码",
                        )
                        email_code_anchored = True
                    await flow.sleep(800)
                    continue
                if not email_code_anchored:
                    since = await _anchor_email_verification_request(
                        mail_provider,
                        account,
                        bad_codes,
                        prefix,
                        reason="进入验证码页",
                    )
                    email_code_anchored = True
                log(f"{prefix} 等待邮箱验证码...")
                for attempt in range(1, 4):
                    try:
                        email_code, since = await _wait_email_code(
                            flow,
                            account,
                            mail_provider,
                            since,
                            bad_codes,
                            prefix,
                            wait_before_resend_sec=CURSOR_EMAIL_CODE_INITIAL_WAIT_SEC if attempt == 1 else None,
                        )
                    except EmailVerificationStepEnded:
                        log(f"{prefix} 页面已离开邮箱验证码，转入手机验证流程")
                        break
                    except TimeoutError as exc:
                        log(f"{prefix} 邮箱验证码超时: {exc}")
                        if attempt >= 3:
                            raise CursorRegisterError("email verification code timed out") from exc
                        if await flow.click_resend_code():
                            since = await _anchor_email_verification_request(
                                mail_provider,
                                account,
                                bad_codes,
                                prefix,
                                reason="点击立即重发",
                            )
                        continue
                    await flow.enter_email_code(email_code)
                    await flow.sleep(CURSOR_STEP_SETTLE_MS)
                    if not await flow.is_email_code_rejected():
                        bad_codes.add(email_code)
                        break
                    bad_codes.add(email_code)
                    log(f"{prefix} 验证码 {email_code} 无效或已使用，排除后重试 ({attempt}/3)")
                    if attempt >= 3:
                        raise CursorRegisterError("email verification code rejected repeatedly")
                    if await flow.click_resend_code():
                        since = await _anchor_email_verification_request(
                            mail_provider,
                            account,
                            bad_codes,
                            prefix,
                            reason="验证码无效后重发",
                        )
                        log(f"{prefix} 已点击「立即重发」，等待新验证码")
                        await flow.sleep(800)
                continue

            if state == "phone":
                unknown_count = 0
                if not sms_provider_ready:
                    raise CursorRegisterError("phone required but HERO_SMS_API_KEY is missing")
                log(f"{prefix} 进入手机号验证页 ({flow.page.url})，准备获取 HeroSMS 号码...")
                async with sms_lock:
                    await _handle_phone_sms(flow, sms_selection, prefix, fill_phone=True)
                continue

            if state == "sms_code":
                unknown_count = 0
                if not sms_provider_ready:
                    raise CursorRegisterError("sms code required but HERO_SMS_API_KEY is missing")
                async with sms_lock:
                    await _handle_phone_sms(flow, sms_selection, prefix, fill_phone=False)
                continue

            unknown_count += 1
            if unknown_count >= 12:
                await flow.screenshot("unknown_state")
                raise CursorRegisterError(f"页面状态无法识别: {flow.page.url}")
            await flow.sleep(800)

        if not await flow.is_registration_complete():
            try:
                await flow.open_settings()
            except Exception:
                pass
            if not await flow.is_registration_complete():
                await flow.screenshot("incomplete")
                raise CursorRegisterError("registration incomplete: did not enter Cursor console/settings")

        async with sms_lock:
            await _mark_reused_phone_success(sms_selection, prefix)

        output_root = cfg.get("cursor", {}).get("output_dir") or CURSOR_OUTPUT_ROOT
        out_file = append_cursor_account(
            account.email,
            account.password or "",
            password,
            first_name,
            last_name,
            output_root=output_root,
        )
        log(f"{prefix} Cursor 注册成功，账号已写入 {out_file}")
        await free_register._finalize_free_pool_account(mail_source, success=True, prefix=prefix)
        return True
    except CursorHumanVerificationError as exc:
        log(f"{prefix} Cursor 注册失败: {exc.__class__.__name__}: {exc}")
        await free_register._finalize_free_pool_account(
            mail_source,
            success=False,
            prefix=prefix,
            failure_reason=str(exc),
        )
        raise
    except Exception as exc:
        log(f"{prefix} Cursor 注册失败: {exc.__class__.__name__}: {exc}")
        await free_register._finalize_free_pool_account(
            mail_source,
            success=False,
            prefix=prefix,
            failure_reason=str(exc),
        )
        return False
    finally:
        await session.__aexit__(None, None, None)


async def run_cursor_register_many(
    cfg: dict[str, Any],
    *,
    count: int,
    workers: int,
    sms_selection: dict[str, object] | None,
    register_mode: str = "browser",
) -> int:
    env = load_env(".env")
    if sms_selection is not None:
        sms_selection.setdefault("cursor_phone_max_reuse", 3)
        sms_selection.setdefault("cursor_phone_first_code_timeout_sec", 120)
        sms_selection.setdefault("cursor_phone_lifetime_sec", 1200)
        sms_selection.setdefault("cursor_phone_number_retries", CURSOR_SMS_PHONE_NUMBER_RETRIES)
        sms_selection.setdefault(
            "cursor_sms_resend_wait_sec",
            _resolve_cursor_sms_resend_wait_sec(sms_selection),
        )
    free_register.reset_free_pool_claims_on_start(cfg, env, prefix="[Cursor]", flow="CURSOR")
    proxy_pool = ProxyPool(cfg.get("browser", {}).get("proxy_file", "data/proxies/proxies.txt")) if cfg.get("browser", {}).get("use_proxy") else None
    success = 0
    attempts = 0
    max_attempts = max(count * int(cfg.get("cursor_register", {}).get("max_attempt_multiplier", 3)), count)
    lock = asyncio.Lock()
    stop = False

    async def worker(worker_id: int) -> None:
        nonlocal success, attempts, stop
        while True:
            async with lock:
                if stop or success >= count or attempts >= max_attempts:
                    return
                attempts += 1
                attempt_no = attempts

            proxy = proxy_pool.pick(attempt_no) if proxy_pool else None
            try:
                mail_source = await free_register.create_free_mail_account(cfg, env, worker_id=worker_id, flow="CURSOR")
            except free_register.FreeRegisterError as exc:
                log(f"[cursor-{worker_id:02d}] 领取邮箱失败: {exc}")
                async with lock:
                    stop = True
                return

            try:
                ok = await run_cursor_register_once(
                    cfg,
                    sms_selection=sms_selection,
                    worker_id=worker_id,
                    proxy=proxy,
                    mail_source=mail_source,
                    register_mode=register_mode,
                )
                if ok:
                    async with lock:
                        success += 1
            except CursorHumanVerificationError as exc:
                log(
                    f"[cursor-{worker_id:02d}] {exc}，"
                    f"暂停 {CURSOR_HUMAN_VERIFY_FAIL_PAUSE_SEC // 60} 分钟后继续下一个邮箱"
                )
                await asyncio.sleep(CURSOR_HUMAN_VERIFY_FAIL_PAUSE_SEC)

    await asyncio.gather(*(worker(i) for i in range(1, max(1, workers) + 1)))
    await _complete_reused_phone_activation(sms_selection)
    log(f"Cursor 注册结束，成功数: {success}/{count}")
    return 0 if success >= count else 1


def interactive_cursor_register(
    config_path: str,
    cfg: dict[str, Any],
    sms_selection: dict[str, object] | None,
    ask_positive_int,
) -> int:
    print()
    print("Cursor 自动注册")
    print("  - 邮箱注册 + 自动获取邮箱验证码")
    print("  - 手机验证 + HeroSMS 自动接码")
    print()
    count = ask_positive_int("请输入这次要成功注册多少个", default=1)
    workers = ask_positive_int("请输入并发线程数", default=1, max_value=count)
    return asyncio.run(run_cursor_register_many(cfg, count=count, workers=workers, sms_selection=sms_selection))

