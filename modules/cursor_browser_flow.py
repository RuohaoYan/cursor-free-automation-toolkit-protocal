from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Callable

from playwright.async_api import Page

from .captcha_auto import (
    _captcha_mode,
    _capsolver_key,
    _headed_captcha_fallback_enabled,
    CAPTCHA_MAX_CLICK_ATTEMPTS,
    CAPTCHA_RETRY_INTERVAL_SEC,
    captcha_strategy_summary,
    is_cloudflare_interstitial,
    security_challenge_visible,
    should_use_capsolver,
    try_auto_clear_security_challenge,
)
from .utils import env_bool, load_env, log, resolve_path, safe_filename


CURSOR_HOME_URL = "https://cursor.com/"
CURSOR_SIGNUP_URL = "https://authenticator.cursor.sh/sign-up"
CURSOR_SETTINGS_URL = "https://www.cursor.com/settings"
CURSOR_DASHBOARD_URL = "https://www.cursor.com/dashboard"

SIGN_IN_LABELS = ("Sign in", "Log in", "Login", "登录")
REGISTER_LABELS = (
    "Sign up",
    "Register",
    "Create account",
    "Sign up for free",
    "Get started",
    "注册",
    "创建账号",
    "免费注册",
)
EMAIL_CODE_METHOD_LABELS = (
    "Email code",
    "Email verification code",
    "Verification code via email",
    "邮箱验证码",
    "邮箱登录验证码",
    "电子邮件验证码",
)
RESEND_EMAIL_CODE_LABELS = (
    "立即重发",
    "Resend now",
    "Resend code",
    "Resend",
    "Send again",
    "重新发送",
    "再次发送",
    "重发",
    "发送验证码",
    "Send code",
)
REQUEST_NEW_SMS_CODE_LABELS = (
    "请求新的验证码",
    "Request a new code",
    "Request a new verification code",
    "Get a new verification code",
)
RESEND_SMS_CODE_LABELS = REQUEST_NEW_SMS_CODE_LABELS
SKIP_LATER_LABELS = (
    "之后再说",
    "稍后再说",
    "稍后",
    "以后再说",
    "晚点再说",
    "暂时跳过",
    "跳过",
    "Maybe later",
    "Remind me later",
    "Do it later",
    "I'll do it later",
    "Not right now",
    "Not now",
    "Skip for now",
    "Skip",
    "Later",
    "Dismiss",
    "Close",
    "Cancel",
    "後で",
)

_EMAIL_INPUT_SELECTORS = (
    'input[type="email"]',
    'input[name="email"]',
    'input[name="emailAddress"]',
    'input[autocomplete="email"]',
    'input[placeholder*="email" i]',
    'input[placeholder*="邮箱" i]',
)

_FIRST_NAME_SELECTORS = (
    'input[name="first_name"]',
    'input[name="firstName"]',
    'input[name="givenName"]',
    'input[autocomplete="given-name"]',
    'input[placeholder*="First name" i]',
    'input[placeholder*="名" i]',
    'input[aria-label*="first name" i]',
)

_LAST_NAME_SELECTORS = (
    'input[name="last_name"]',
    'input[name="lastName"]',
    'input[name="familyName"]',
    'input[autocomplete="family-name"]',
    'input[placeholder*="Last name" i]',
    'input[placeholder*="姓" i]',
    'input[aria-label*="last name" i]',
)

_FULL_NAME_SELECTORS = (
    'input[name="name"]',
    'input[name="full_name"]',
    'input[name="fullName"]',
    'input[placeholder*="Full name" i]',
    'input[placeholder*="姓名" i]',
)

_OTP_EXCLUDED_INPUT_NAMES = frozenset(
    {
        "phone_number",
        "country_code",
        "local_number",
        "cf-turnstile-response",
    }
)


class CursorBrowserFlow:
    """Browser helpers for cursor.com → authenticator signup flow."""

    def __init__(self, page: Page, prefix: str) -> None:
        self.page = page
        self.prefix = prefix

    def say(self, message: str) -> None:
        log(f"{self.prefix} {message}")

    async def sleep(self, ms: int) -> None:
        await self.page.wait_for_timeout(ms)

    async def _goto_with_retry(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        timeout: int = 60_000,
        retries: int = 3,
    ) -> None:
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                await self.page.goto(url, wait_until=wait_until, timeout=timeout)
                return
            except Exception as exc:
                last_exc = exc
                msg = str(exc).lower()
                transient = any(
                    token in msg
                    for token in (
                        "err_connection_closed",
                        "err_connection_reset",
                        "err_empty_response",
                        "err_proxy",
                        "err_network",
                        "err_timed_out",
                        "timeout",
                    )
                )
                if attempt < retries and transient:
                    self.say(
                        f"[Browser] 页面加载失败 ({attempt}/{retries})，{exc.__class__.__name__}，重试..."
                    )
                    await self.sleep(1500 * attempt)
                    continue
                raise
        if last_exc:
            raise last_exc

    async def _settle(self) -> None:
        await self.sleep(500)

    async def _click_by_labels(self, labels: tuple[str, ...]) -> bool:
        patterns = [re.compile(re.escape(label), re.I) for label in labels if label]
        for pattern in patterns:
            for role in ("button", "link"):
                locators = self.page.get_by_role(role, name=pattern)
                for index in range(await locators.count()):
                    item = locators.nth(index)
                    try:
                        if await item.is_visible() and await item.is_enabled():
                            await item.click(timeout=5000)
                            await self._settle()
                            return True
                    except Exception:
                        continue
        clicked = await self.page.evaluate(
            """(labels) => {
                const normalized = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const wanted = labels.map(normalized);
                const nodes = [...document.querySelectorAll('button, a, [role="button"], [role="link"]')];
                for (const node of nodes) {
                    const text = normalized(node.innerText || node.textContent || node.getAttribute('aria-label') || '');
                    if (!text || !wanted.some((label) => text === label || text.includes(label))) continue;
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    if (rect.width <= 0 || rect.height <= 0 || style.visibility === 'hidden' || style.display === 'none') continue;
                    node.click();
                    return true;
                }
                return false;
            }""",
            list(labels),
        )
        if clicked:
            await self._settle()
            return True
        return False

    def _is_cursor_home(self, url: str) -> bool:
        low = (url or "").lower()
        if "authenticator.cursor" in low:
            return False
        if any(
            part in low
            for part in (
                "cursor.com/dashboard",
                "cursor.com/settings",
                "cursor.com/api/auth",
            )
        ):
            return False
        return low.rstrip("/").endswith("cursor.com") or bool(
            re.search(r"cursor\.com/(?:en-us)?/?$", low)
        )

    def _is_authenticator(self, url: str) -> bool:
        return "authenticator.cursor" in (url or "").lower()

    async def _wait_for_authenticator(self, timeout_ms: int = 60_000) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        cf_handled = False
        while time.monotonic() < deadline:
            url = self.page.url or ""
            if self._is_authenticator(url):
                if not cf_handled:
                    self.say(f"[Browser] 已进入认证页: {url}")
                    cf_timeout = 90_000 if await is_cloudflare_interstitial(self.page) else 60_000
                    await self.wait_for_cloudflare(
                        min(cf_timeout, int((deadline - time.monotonic()) * 1000))
                    )
                    cf_handled = True
                return True
            await self.sleep(1000)
        return self._is_authenticator(self.page.url or "")

    async def _wait_for_signup_form(self, timeout_ms: int = 45_000) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if await self._signup_form_ready():
                return True
            await self.sleep(1000)
        return await self._signup_form_ready()

    async def _resolve_sign_in_entry_url(self) -> str:
        try:
            href = await self.page.evaluate(
                """() => {
                    const anchors = [...document.querySelectorAll('a[href]')];
                    for (const anchor of anchors) {
                        const text = (anchor.innerText || anchor.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                        if (/^(sign in|log in|login|登录)$/.test(text)) {
                            return anchor.href || '';
                        }
                    }
                    return '';
                }"""
            )
        except Exception:
            href = ""
        href = str(href or "").strip()
        if href and "cursor.com" in href.lower():
            return href
        return "https://cursor.com/dashboard"

    async def _goto_auth_entry(self) -> None:
        url = self.page.url or ""
        if self._is_authenticator(url):
            return
        target = await self._resolve_sign_in_entry_url()
        self.say(f"[Browser] 进入登录入口: {target}")
        await self._goto_with_retry(target, wait_until="domcontentloaded", timeout=60_000)
        if not await self._wait_for_authenticator(60_000):
            raise RuntimeError(f"未能进入 Cursor 认证页: {self.page.url}")

    async def _build_signup_url(self) -> str:
        current = self.page.url or ""
        if "?" in current and self._is_authenticator(current):
            return f"{CURSOR_SIGNUP_URL}?{current.split('?', 1)[1]}"
        return CURSOR_SIGNUP_URL

    async def _goto_signup_from_auth(self) -> bool:
        if not self._is_authenticator(self.page.url or ""):
            return False
        if await self._signup_form_ready():
            return True
        if await self.click_welcome_register():
            return await self._wait_for_signup_form(30_000)
        signup_url = await self._build_signup_url()
        self.say(f"[Browser] 打开注册页: {signup_url}")
        await self._goto_with_retry(signup_url, wait_until="domcontentloaded", timeout=60_000)
        await self.wait_for_cloudflare(60_000)
        return await self._wait_for_signup_form(45_000)

    async def _is_auth_welcome(self) -> bool:
        url = (self.page.url or "").lower()
        if "authenticator.cursor" not in url:
            return False
        if await self._signup_form_ready():
            return False
        if await self._find_input('input[name="password"], input[type="password"]'):
            return False
        text = (await self._body_text()).lower()
        hints = (
            "welcome",
            "sign in to cursor",
            "log in to cursor",
            "get started",
            "don't have an account",
            "欢迎",
            "登录 cursor",
            "还没有账号",
            "没有账号",
        )
        return any(h in text for h in hints) or await self._clickable_register_visible()

    async def _clickable_register_visible(self) -> bool:
        for label in REGISTER_LABELS[:4]:
            try:
                loc = self.page.get_by_role("button", name=re.compile(re.escape(label), re.I)).first
                if await loc.count() > 0 and await loc.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def click_home_sign_in(self) -> bool:
        if self._is_authenticator(self.page.url or ""):
            return True
        await self._goto_auth_entry()
        return True

    async def click_welcome_register(self) -> bool:
        if await self._signup_form_ready():
            return True
        if await self._click_by_labels(REGISTER_LABELS):
            self.say("[Browser] 已在欢迎页点击注册")
            await self.wait_for_cloudflare(45_000)
            return True
        return False

    async def _ensure_on_signup_form(self) -> None:
        if not self._is_authenticator(self.page.url or ""):
            await self._goto_auth_entry()
        if await self._signup_form_ready():
            self.say("[Browser] 注册表单已就绪")
            return
        if await self._goto_signup_from_auth():
            self.say("[Browser] 注册表单已就绪")
            return

        deadline = time.monotonic() + 90
        register_attempts = 0
        while time.monotonic() < deadline:
            if await self._signup_form_ready():
                self.say("[Browser] 注册表单已就绪")
                return
            if self._is_cursor_home(self.page.url or ""):
                await self._goto_auth_entry()
            elif self._is_authenticator(self.page.url or ""):
                if register_attempts < 2 and await self._goto_signup_from_auth():
                    self.say("[Browser] 注册表单已就绪")
                    return
                register_attempts += 1
            await self.sleep(800)
        raise RuntimeError(f"未能进入 Cursor 注册表单: {self.page.url}")

    async def navigate_to_signup(self, url: str = CURSOR_HOME_URL) -> None:
        entry = (url or CURSOR_HOME_URL).strip()
        self.say(f"[Browser] open: {entry}")
        await self._goto_with_retry(entry, wait_until="domcontentloaded", timeout=60_000)
        await self.wait_for_cloudflare(60_000)
        if "authenticator.cursor" in entry.lower() and "sign-up" in entry.lower():
            if await self._signup_form_ready():
                self.say("[Browser] 注册表单已就绪")
            return
        if not self._is_authenticator(self.page.url or ""):
            await self._goto_auth_entry()
        self.say("[Browser] 已进入认证欢迎页")

    async def screenshot(self, filename: str) -> None:
        out = resolve_path("output/cursor_register/debug")
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{int(time.time())}_{safe_filename(self.prefix)[-80:]}_{filename}"
        await self.page.screenshot(path=str(path), full_page=True)
        self.say(f"[Browser] screenshot: {path}")

    async def _find_any_input(self, selectors: tuple[str, ...]):
        for selector in selectors:
            found = await self._find_input(selector)
            if found:
                return found
        return None

    async def _signup_form_ready(self) -> bool:
        url = (self.page.url or "").lower()
        if "authenticator.cursor" not in url:
            return False
        email = await self._find_any_input(_EMAIL_INPUT_SELECTORS)
        if not email:
            return False
        first = await self._find_any_input(_FIRST_NAME_SELECTORS)
        last = await self._find_any_input(_LAST_NAME_SELECTORS)
        if first or last:
            return True
        return await self._find_any_input(_FULL_NAME_SELECTORS) is not None

    async def _wait_post_captcha_ready(self, timeout_sec: float = 25) -> bool:
        from .captcha_auto import _challenge_navigated_past_cf, turnstile_token_ready

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if await self._interactive_signup_step_ready():
                return True
            if await self._is_auth_welcome():
                return True
            if await _challenge_navigated_past_cf(self.page):
                return True
            if await turnstile_token_ready(self.page):
                await asyncio.sleep(1.5)
                if await self._interactive_signup_step_ready() or await _challenge_navigated_past_cf(self.page):
                    return True
            visible, _ = await security_challenge_visible(self.page)
            if not visible:
                return True
            await self.sleep(400)
        return False

    async def wait_for_cloudflare(self, timeout_ms: int = 60_000) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        capsolver_tried = False
        strategy_logged = False
        reloaded_once = False
        headless = env_bool(
            (env := load_env(".env")).get("CURSOR_BROWSER_HEADLESS") or env.get("BROWSER_HEADLESS"),
            default=False,
        )
        if headless and await is_cloudflare_interstitial(self.page):
            timeout_ms = min(timeout_ms, 50_000)
        while True:
            last_log = 0.0
            click_attempts = 0
            last_click_at = 0.0
            settle_until = 0.0
            while time.monotonic() < deadline:
                if await self._interactive_signup_step_ready():
                    return
                if await self._signup_form_ready():
                    return
                visible, reason = await security_challenge_visible(self.page)
                if not visible:
                    return
                if not strategy_logged:
                    self.say(f"[Captcha] 安全验证策略: {captcha_strategy_summary()}")
                    strategy_logged = True
                now = time.monotonic()
                if now < settle_until:
                    if await self._wait_post_captcha_ready(min(3.0, settle_until - now)):
                        self.say("[Browser] 安全验证已自动通过")
                        return
                    await self.sleep(300)
                    continue
                from .captcha_auto import turnstile_token_ready

                token_pending = await turnstile_token_ready(self.page)
                if token_pending:
                    if await self._wait_post_captcha_ready(20):
                        self.say("[Browser] Turnstile token 已就绪，安全验证已通过")
                        return
                    settle_until = time.monotonic() + 15
                    await self.sleep(500)
                    continue
                can_click = click_attempts == 0 or (
                    click_attempts < CAPTCHA_MAX_CLICK_ATTEMPTS
                    and now - last_click_at >= CAPTCHA_RETRY_INTERVAL_SEC
                )
                if can_click:
                    click_attempts += 1
                    last_click_at = now
                    if await try_auto_clear_security_challenge(
                        self.page,
                        self.say,
                        try_capsolver=not capsolver_tried,
                        attempt=click_attempts,
                        max_attempts=CAPTCHA_MAX_CLICK_ATTEMPTS,
                        log_strategy=click_attempts <= 1,
                    ):
                        if not capsolver_tried and should_use_capsolver() and _capsolver_key():
                            capsolver_tried = True
                        if await self._wait_post_captcha_ready(25):
                            self.say("[Browser] 安全验证已自动通过")
                            return
                        # 点击后等待页面跳转，避免立刻再次点击把 Turnstile 重置
                        settle_until = time.monotonic() + 12
                        continue
                    elif not capsolver_tried and should_use_capsolver() and _capsolver_key():
                        capsolver_tried = True
                    if (
                        headless
                        and _headed_captcha_fallback_enabled()
                        and click_attempts >= CAPTCHA_MAX_CLICK_ATTEMPTS
                    ):
                        break
                now = time.monotonic()
                if now - last_log >= 5:
                    self.say(
                        f"[Browser] 等待安全验证通过 ({reason})..."
                        f" Captcha {click_attempts}/{CAPTCHA_MAX_CLICK_ATTEMPTS}"
                    )
                    last_log = now
                await self.sleep(300)

            visible, reason = await security_challenge_visible(self.page)
            if not visible:
                return
            if headless and _headed_captcha_fallback_enabled():
                self.say("[Browser] 无头 Captcha 未通过，切换有界面模式重试")
                raise RuntimeError(f"无头模式安全验证未通过: {reason or self.page.url}")
            if headless and not reloaded_once:
                reloaded_once = True
                self.say("[Browser] 无头 Captcha 未通过，刷新认证页重试一次")
                try:
                    await self.page.reload(wait_until="domcontentloaded", timeout=60_000)
                except Exception as exc:
                    self.say(f"[Browser] 刷新失败: {exc.__class__.__name__}: {exc}")
                else:
                    await self.sleep(2000)
                    deadline = max(deadline, time.monotonic() + 45)
                    continue
            if headless and click_attempts >= CAPTCHA_MAX_CLICK_ATTEMPTS:
                break
            break

        visible, reason = await security_challenge_visible(self.page)
        if not visible:
            return
        if _captcha_mode() == "api":
            raise RuntimeError(f"安全验证自动求解未通过: {reason or self.page.url}")
        if headless:
            raise RuntimeError(f"无头模式安全验证未通过: {reason or self.page.url}")
        self.say(f"[Browser] 安全验证仍未通过 ({reason})，请在浏览器窗口手动完成验证")
        manual_deadline = time.monotonic() + 90
        while time.monotonic() < manual_deadline:
            visible, reason = await security_challenge_visible(self.page)
            if not visible:
                self.say("[Browser] 安全验证已通过")
                return
            await self.sleep(1000)
        raise RuntimeError(f"安全验证超时: {reason or self.page.url}")

    async def _body_text(self) -> str:
        try:
            return await self.page.evaluate("() => document.body?.innerText || ''")
        except Exception:
            return ""

    async def _needs_name_email_fill(self) -> bool:
        email = await self._find_any_input(_EMAIL_INPUT_SELECTORS)
        if not email:
            return False
        try:
            if not await email.is_visible():
                return False
            email_val = (await email.input_value()).strip()
            if not email_val:
                return True
        except Exception:
            return True
        first = await self._find_any_input(_FIRST_NAME_SELECTORS)
        if first:
            try:
                if await first.is_visible() and await first.is_enabled():
                    if not (await first.input_value()).strip():
                        return True
            except Exception:
                return True
        last = await self._find_any_input(_LAST_NAME_SELECTORS)
        if last:
            try:
                if await last.is_visible() and await last.is_enabled():
                    if not (await last.input_value()).strip():
                        return True
            except Exception:
                return True
        return False

    async def _is_password_step(self) -> bool:
        pw = await self._find_input('input[name="password"], input[type="password"]')
        if not pw:
            return False
        try:
            if not await pw.is_visible():
                return False
        except Exception:
            return False

        low = (await self._body_text()).lower()
        if any(
            hint in low
            for hint in (
                "create a password",
                "create password",
                "create your password",
                "choose a password",
                "your password",
                "设置密码",
                "创建密码",
                "输入密码",
                "请设置密码",
                "创建您的密码",
            )
        ):
            return True

        first = await self._find_any_input(_FIRST_NAME_SELECTORS)
        if first:
            try:
                if await first.is_visible() and await first.is_enabled():
                    if not (await first.input_value()).strip():
                        return False
            except Exception:
                pass

        email = await self._find_any_input(_EMAIL_INPUT_SELECTORS)
        if email:
            try:
                if await email.is_visible() and await email.is_enabled():
                    email_val = (await email.input_value()).strip()
                    if not email_val:
                        return False
                    if "@" in email_val:
                        return True
            except Exception:
                pass

        return not await self._needs_name_email_fill()

    async def _is_auth_email_step(self) -> bool:
        if not self._is_authenticator(self.page.url or ""):
            return False
        if await self._signup_form_ready():
            return False
        if await self._is_password_step():
            return False
        if await self._has_email_code_inputs():
            return False
        email = await self._find_any_input(_EMAIL_INPUT_SELECTORS)
        if not email:
            return False
        try:
            return await email.is_visible() and await email.is_enabled()
        except Exception:
            return True

    async def _needs_auth_email_fill(self) -> bool:
        email = await self._find_any_input(_EMAIL_INPUT_SELECTORS)
        if not email:
            return False
        try:
            return not (await email.input_value()).strip()
        except Exception:
            return True

    def _is_radar_phone_url(self, url: str | None = None) -> bool:
        return "radar-challenge" in ((url or self.page.url or "").lower())

    async def _has_split_phone_form(self) -> bool:
        try:
            return bool(
                await self.page.evaluate(
                    """() => {
                        const cc = document.querySelector('input[name="country_code"]');
                        const ln = document.querySelector('input[name="local_number"]');
                        return !!(cc && ln);
                    }"""
                )
            )
        except Exception:
            return False

    async def _is_phone_verification_page(self) -> bool:
        url = (self.page.url or "").lower()
        if "radar-challenge/send" in url or ("/send" in url and "radar-challenge" in url):
            return True
        if await self._has_split_phone_form():
            return True
        phone_input = await self._find_phone_input()
        if not phone_input:
            return False
        low = (await self._body_text()).lower()
        phone_hints = (
            "verify your phone",
            "phone verification",
            "phone number",
            "mobile number",
            "验证您的手机",
            "手机号码",
            "电话号码",
        )
        if any(h in low for h in ("check your email", "verify your email", "email code", "邮箱验证", "电子邮件")):
            return False
        return any(h in low for h in phone_hints)

    def _url_is_phone_challenge(self, url: str | None = None) -> bool:
        return "radar-challenge" in ((url or self.page.url or "").lower())

    async def _is_phone_otp_step(self) -> bool:
        url = (self.page.url or "").lower()
        if "magic-code" in url:
            return False
        low = (await self._body_text()).lower()
        if any(h in low for h in ("check your email", "verify your email", "email code", "邮箱验证", "电子邮件")):
            return False
        phone_otp_hints = (
            "verify your phone",
            "sent to your phone",
            "sent to +",
            "text message",
            "短信",
            "验证您的手机",
        )
        return any(h in low for h in phone_otp_hints) and (
            "phone" in low or "手机" in low or "短信" in low or "text message" in low
        )

    async def _detect_magic_code_state(self, low: str) -> str | None:
        url = (self.page.url or "").lower()
        if "magic-code" not in url:
            return None
        if await self._has_split_phone_form() or await self._find_phone_input():
            return "phone"
        has_otp = False
        try:
            has_otp = await self.page.locator('input[data-index="0"]').count() > 0
        except Exception:
            pass
        phone_sms = any(
            h in low
            for h in (
                "verify your phone",
                "验证您的手机",
                "sent to your phone",
                "sent to +",
                "text message",
                "短信验证",
                "手机号码",
            )
        ) and not any(
            h in low
            for h in (
                "check your email",
                "verify your email",
                "email code",
                "邮箱验证",
                "电子邮件",
                "email",
                "邮箱",
            )
        )
        if phone_sms and has_otp:
            return "sms_code"
        if has_otp:
            return "email_code"
        if any(
            h in low
            for h in (
                "check your email",
                "verify your email",
                "email code",
                "邮箱验证",
                "邮箱验证码",
                "magic code",
            )
        ):
            return "email_code_pending"
        return "email_code_pending"

    async def _is_email_code_method_page(self) -> bool:
        if not self._is_authenticator(self.page.url or ""):
            return False
        if "magic-code" in (self.page.url or "").lower():
            return False
        if await self._is_phone_verification_page():
            return False
        if "radar-challenge" in (self.page.url or "").lower():
            return False
        if await self._has_email_code_inputs():
            return False
        low = (await self._body_text()).lower()
        if any(h in low for h in ("phone number", "mobile number", "verify your phone", "手机号", "电话号码", "短信验证")):
            return False
        hints = (
            "邮箱登录验证码",
            "email code",
            "email verification",
            "verify your email",
            "check your email",
            "magic link",
        )
        if any(h in low for h in hints):
            return True
        generic_hints = (
            "verification code",
            "enter the code",
            "输入验证码",
            "send code",
            "we sent",
            "one-time code",
        )
        return any(h in low for h in generic_hints) and "email" in low

    async def fill_auth_email(self, email: str) -> None:
        email_input = await self._find_any_input(_EMAIL_INPUT_SELECTORS)
        if not email_input:
            raise RuntimeError("未找到欢迎页邮箱输入框")
        current = ""
        try:
            current = (await email_input.input_value()).strip()
        except Exception:
            pass
        if current.lower() != email.lower():
            await self._human_fill(email_input, email)
        self.say(f"[Browser] 欢迎页填写邮箱: {email}")
        submit_labels = [
            "Continue",
            "Next",
            "Submit",
            "Get started",
            "Sign in",
            "Log in",
            "Send code",
            "Verify",
            "继续",
            "下一步",
            "登录",
            "发送",
            "验证",
        ]
        for attempt in range(1, 4):
            if not await self._is_auth_email_step():
                return
            if await self.click_submit(submit_labels):
                await self._settle()
                if not await self._is_auth_email_step():
                    return
            if await self._click_by_labels(tuple(submit_labels)):
                await self._settle()
                if not await self._is_auth_email_step():
                    return
            if await self._submit_email_form_js():
                await self._settle()
                if not await self._is_auth_email_step():
                    return
            try:
                await email_input.press("Enter")
                await self._settle()
                if not await self._is_auth_email_step():
                    return
            except Exception:
                pass
            if attempt < 3:
                self.say(f"[Browser] 欢迎页继续按钮未响应，重试 ({attempt}/3)")
                await self.sleep(900)
        if not await self._is_auth_email_step():
            return
        raise RuntimeError("欢迎页未找到继续按钮")

    async def _submit_email_form_js(self) -> bool:
        try:
            return bool(
                await self.page.evaluate(
                    """() => {
                        const email = document.querySelector(
                            'input[type="email"], input[name="email"], input[name="emailAddress"]'
                        );
                        if (!email) return false;
                        const visible = (el) => {
                            const r = el.getBoundingClientRect();
                            const st = getComputedStyle(el);
                            return r.width > 0 && r.height > 0 &&
                                st.visibility !== 'hidden' && st.display !== 'none' && !el.disabled;
                        };
                        const form = email.closest('form');
                        if (form) {
                            const buttons = [...form.querySelectorAll('button')].filter(visible);
                            const submit = buttons.find((b) =>
                                (b.getAttribute('type') || '').toLowerCase() !== 'button'
                            ) || buttons[0];
                            if (submit) {
                                submit.click();
                                return true;
                            }
                            try { form.requestSubmit(); return true; } catch {}
                        }
                        const labels = [
                            'continue', 'next', 'submit', 'sign in', 'log in', 'get started',
                            'send code', 'verify', '继续', '下一步', '登录', '发送', '验证'
                        ];
                        const nodes = [...document.querySelectorAll('button, [role="button"]')];
                        for (const node of nodes) {
                            if (!visible(node)) continue;
                            const text = (node.innerText || node.textContent || node.getAttribute('aria-label') || '')
                                .replace(/\\s+/g, ' ').trim().toLowerCase();
                            if (!text) continue;
                            if (labels.some((label) => text === label || text.includes(label))) {
                                node.click();
                                return true;
                            }
                        }
                        return false;
                    }"""
                )
            )
        except Exception:
            return False

    async def click_email_code_method(self) -> bool:
        if await self._click_by_labels(EMAIL_CODE_METHOD_LABELS):
            self.say("[Browser] 已选择邮箱验证码登录/验证")
            return True
        return False

    async def _interactive_signup_step_ready(self) -> bool:
        if await self._is_auth_welcome():
            return True
        if await self._is_password_step():
            return True
        if await self._is_auth_email_step():
            return True
        if await self._is_email_code_method_page():
            return True
        return await self._signup_form_ready() and await self._needs_name_email_fill()

    async def detect_state(self) -> str:
        url = (self.page.url or "").lower()
        text = await self._body_text()
        low = text.lower()

        if "this email is not available" in low or "email is not available" in low:
            return "email_unavailable"

        if any(h in low for h in ("account settings", "账户设置", "dashboard", "usage")):
            if "cursor.com" in url or "authenticator.cursor" in url:
                return "success"

        if "cursor.com/dashboard" in url or "cursor.com/settings" in url:
            return "success"

        if self._is_cursor_home(url):
            return "home"

        magic_state = await self._detect_magic_code_state(low)
        if magic_state:
            return magic_state

        if "radar-challenge" in url:
            if "/send" in url or await self._has_split_phone_form():
                return "phone"
            if await self._is_phone_otp_step() or await self._has_sms_code_inputs():
                return "sms_code"
            return "phone"

        if await self._is_phone_verification_page():
            return "phone"

        if await self._find_phone_input():
            return "phone"

        if await self._is_phone_otp_step() or await self._has_sms_code_inputs():
            return "sms_code"

        if await self._has_email_code_inputs():
            return "email_code"

        if await self._is_email_code_method_page():
            return "email_code_method"

        if await self._is_auth_email_step():
            return "auth_email"

        if self._is_authenticator(url) and not await self._signup_form_ready():
            if await self._is_auth_welcome():
                return "auth_email"

        if await self._is_password_step():
            return "password"

        if await self._needs_name_email_fill() and await self._signup_form_ready():
            return "name_email"

        if await self._find_input('input[name="password"], input[type="password"]'):
            return "password"

        if await self._find_phone_input():
            return "phone"

        if await self._has_sms_code_inputs():
            return "sms_code"

        if any(
            h in low
            for h in (
                "check your email",
                "verification code",
                "enter the code",
                "email code",
                "输入验证码",
                "邮箱验证码",
                "验证码",
            )
        ):
            if await self._has_email_code_inputs():
                return "email_code"
            return "email_code_pending"

        visible, _ = await security_challenge_visible(self.page)
        if visible:
            if await self._is_password_step():
                return "password"
            if await self._is_phone_verification_page():
                return "phone"
            if await self._is_auth_email_step() or await self._is_email_code_method_page():
                return "auth_email" if await self._is_auth_email_step() else "email_code_method"
            if await self._needs_name_email_fill() and await self._signup_form_ready():
                return "name_email"
            return "security_challenge"

        return "unknown"

    async def _find_input(self, selector: str):
        try:
            loc = self.page.locator(selector).first
            if await loc.count() > 0 and await loc.is_visible():
                return loc
        except Exception:
            pass
        return None

    async def _locator_is_editable_otp(self, loc) -> bool:
        try:
            if await loc.count() == 0:
                return False
            return bool(
                await loc.evaluate(
                    """(el, excluded) => {
                        if (!el || el.disabled || el.readOnly) return false;
                        if (el.type === 'hidden') return false;
                        if (excluded.includes(el.name || '')) return false;
                        const r = el.getBoundingClientRect();
                        const st = getComputedStyle(el);
                        if (r.width <= 0 || r.height <= 0) return false;
                        if (st.visibility === 'hidden' || st.display === 'none') return false;
                        return true;
                    }""",
                    list(_OTP_EXCLUDED_INPUT_NAMES),
                )
            )
        except Exception:
            return False

    async def _collect_otp_digit_locators(self, *, min_digits: int = 4) -> list:
        indexed: list = []
        for i in range(8):
            loc = self.page.locator(f'input[data-index="{i}"]').first
            if not await self._locator_is_editable_otp(loc):
                break
            indexed.append(loc)
        if len(indexed) >= min_digits:
            return indexed

        for selector in (
            'input[autocomplete="one-time-code"]',
            'input[name*="code" i]:not([type="hidden"])',
        ):
            loc = self.page.locator(selector).first
            if await self._locator_is_editable_otp(loc):
                return [loc]

        url = (self.page.url or "").lower()
        if any(token in url for token in ("magic-code", "verification", "challenge", "radar-challenge")):
            all_inputs = self.page.locator(
                'input:not([type="password"]):not([type="email"]):not([type="hidden"])'
            )
            visible: list = []
            count = await all_inputs.count()
            for i in range(min(count, 12)):
                loc = all_inputs.nth(i)
                if await self._locator_is_editable_otp(loc):
                    visible.append(loc)
            if len(visible) >= min_digits:
                return visible[:8]
        return []

    async def _fill_otp_digits(self, locators: list, digits: str) -> None:
        if len(locators) == 1:
            await self._human_fill(locators[0], digits)
            return
        for i, ch in enumerate(digits):
            if i >= len(locators):
                break
            await locators[i].fill(ch)
            await self.sleep(random.randint(80, 200))

    async def _find_phone_input(self):
        selectors = (
            'input[name="local_number"]',
            'input[type="tel"]',
            'input[name="phoneNumberInput"]',
            'input[name*="phone" i]',
            'input[autocomplete="tel"]',
            'input[placeholder*="phone" i]',
            'input[placeholder*="手机" i]',
            'input[placeholder*="电话" i]',
            'input[inputmode="tel"]',
        )
        for selector in selectors:
            found = await self._find_input(selector)
            if found:
                return found
        return None

    async def _has_email_code_inputs(self) -> bool:
        if await self._has_split_phone_form():
            return False
        if await self._find_phone_input():
            return False
        locators = await self._collect_otp_digit_locators(min_digits=4)
        return len(locators) >= 4 or len(locators) == 1

    async def is_still_email_verification_step(self) -> bool:
        state = await self.detect_state()
        return state in {"email_code", "email_code_pending", "email_code_method"}

    async def _has_sms_code_inputs(self) -> bool:
        try:
            body = (await self._body_text()).lower()
            if not any(h in body for h in ("sms", "text message", "短信", "phone code", "mobile")):
                return False
            locators = await self._collect_otp_digit_locators(min_digits=4)
            return len(locators) >= 4 or len(locators) == 1
        except Exception:
            return False

    async def _human_fill(self, locator, value: str) -> None:
        await locator.scroll_into_view_if_needed(timeout=3000)
        await locator.click(timeout=3000)
        await locator.fill("")
        for ch in value:
            await locator.type(ch, delay=random.randint(40, 120))
        await self.sleep(random.randint(300, 700))

    async def fill_name_email(self, first_name: str, last_name: str, email: str) -> None:
        if not await self._needs_name_email_fill():
            self.say("[Browser] 姓名/邮箱已填写，跳过")
            return
        first = await self._find_any_input(_FIRST_NAME_SELECTORS)
        last = await self._find_any_input(_LAST_NAME_SELECTORS)
        full = await self._find_any_input(_FULL_NAME_SELECTORS)
        email_input = await self._find_any_input(_EMAIL_INPUT_SELECTORS)
        if not email_input:
            raise RuntimeError("未找到邮箱输入框")
        if full and not first and not last:
            await self._human_fill(full, f"{first_name} {last_name}".strip())
        elif first and last:
            await self._human_fill(first, first_name)
            await self._human_fill(last, last_name)
        elif first:
            await self._human_fill(first, first_name)
            if last:
                await self._human_fill(last, last_name)
        else:
            raise RuntimeError("未找到姓名输入框")
        await self._human_fill(email_input, email)
        await self.click_submit(["Continue", "Next", "Sign up", "Create account", "继续", "下一步", "注册", "创建"])

    async def fill_password(self, password: str) -> None:
        pw = await self._find_input('input[name="password"], input[type="password"]')
        if not pw:
            raise RuntimeError("未找到密码输入框")
        self.say("[Browser] 填写创建密码")
        await self._human_fill(pw, password)
        confirm = await self._find_input(
            'input[name="password_confirmation"], input[name="confirmPassword"], input[name*="confirm" i][type="password"]'
        )
        if confirm:
            await self._human_fill(confirm, password)
        clicked = await self.click_submit(
            ["Continue", "Next", "Sign up", "Create account", "继续", "下一步", "注册", "创建", "完成"]
        )
        if not clicked:
            raise RuntimeError("未找到密码页继续按钮")

    async def click_submit(self, labels: list[str]) -> bool:
        for label in labels:
            try:
                btn = self.page.get_by_role("button", name=re.compile(re.escape(label), re.I)).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=5000)
                    return True
            except Exception:
                pass
        for label in labels:
            try:
                btn = self.page.locator(f'button[type="submit"]:has-text("{label}")').first
                if await btn.count() > 0:
                    await btn.click(timeout=5000)
                    return True
            except Exception:
                pass
        for label in labels:
            try:
                btn = self.page.locator(f'button:has-text("{label}")').first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=5000)
                    return True
            except Exception:
                pass
        if await self._click_by_labels(tuple(labels)):
            return True
        try:
            btn = self.page.locator('button[type="submit"]').first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(timeout=5000)
                return True
        except Exception:
            pass
        return False

    async def enter_email_code(self, code: str) -> None:
        digits = re.sub(r"\D", "", code)[:6]
        if len(digits) < 4:
            raise RuntimeError(f"邮箱验证码格式无效: {code}")
        self.say(f"[Browser] 填入邮箱验证码: {digits}")
        locators = await self._collect_otp_digit_locators(min_digits=4)
        if not locators:
            raise RuntimeError("未找到邮箱验证码输入框")
        await self._fill_otp_digits(locators, digits)

    async def enter_sms_code(self, code: str, logger: Callable[[str], None] | None = None) -> None:
        say = logger or self.say
        digits = re.sub(r"\D", "", code)
        say(f"[Browser] 填入短信验证码: {digits}")
        locators = await self._collect_otp_digit_locators(min_digits=4)
        if not locators:
            raise RuntimeError("未找到短信验证码输入框")
        await self._fill_otp_digits(locators, digits)

    async def is_still_sms_verification_step(self) -> bool:
        state = await self.detect_state()
        return state == "sms_code"

    async def is_sms_code_rejected(self) -> bool:
        try:
            text = (await self.page.inner_text("body")).lower()
        except Exception:
            return False
        hints = (
            "无效的一次性代码",
            "一次性代码无效",
            "invalid one-time",
            "invalid code",
            "incorrect code",
            "code is incorrect",
            "wrong code",
            "expired",
            "try again",
            "代码不正确",
            "验证码不正确",
            "验证码错误",
            "无效代码",
            "已过期",
        )
        return any(hint in text for hint in hints)

    async def wait_sms_code_result(self, timeout_sec: int = 10) -> str:
        for _ in range(max(1, timeout_sec * 2)):
            if await self.is_sms_code_rejected():
                return "rejected"
            if await self.is_registration_complete() or await self.has_skip_later_prompt():
                return "accepted"
            if not await self.is_still_sms_verification_step():
                return "accepted"
            await self.sleep(500)
        if await self.is_sms_code_rejected():
            return "rejected"
        return "pending"

    async def click_request_new_sms_code(self) -> bool:
        if await self._click_by_labels(REQUEST_NEW_SMS_CODE_LABELS):
            self.say("[Browser] 已点击请求新的验证码")
            return True
        return False

    async def is_human_verification_blocked(self) -> bool:
        try:
            text = (await self._body_text()).lower()
        except Exception:
            return False
        hints = (
            "无法验证用户为真人",
            "无法验证您为真人",
            "无法验证你是真人",
            "无法验证您是人",
            "unable to verify you are human",
            "could not verify you are human",
            "cannot verify you are human",
            "couldn't verify you are human",
            "we couldn't verify you are human",
        )
        return any(hint in text for hint in hints)

    async def is_phone_verification_rate_limited(self) -> bool:
        try:
            text = await self._body_text()
        except Exception:
            return False
        low = text.lower()
        hints = (
            "此手机号码的验证请求过多",
            "手机号码的验证请求过多",
            "该手机号码的验证请求过多",
            "too many verification requests for this phone",
            "too many requests for this phone number",
        )
        if any(h in text or h in low for h in hints):
            return True
        return "验证请求过多" in text and "管理员" in text

    async def is_email_code_rejected(self) -> bool:
        try:
            text = (await self.page.inner_text("body")).lower()
        except Exception:
            return False
        hints = (
            "invalid code",
            "incorrect code",
            "code is incorrect",
            "wrong code",
            "expired",
            "try again",
            "代码不正确",
            "验证码不正确",
            "验证码错误",
            "无效代码",
            "已过期",
        )
        return any(hint in text for hint in hints)

    async def click_resend_code(self) -> bool:
        if await self._click_by_labels(RESEND_EMAIL_CODE_LABELS):
            self.say("[Browser] 已点击立即重发/重发验证码")
            return True
        return False

    async def click_resend_sms_code(self) -> bool:
        return await self.click_request_new_sms_code()

    async def has_skip_later_prompt(self) -> bool:
        try:
            text = (await self._body_text()).lower()
        except Exception:
            return False
        return any(label.lower() in text for label in SKIP_LATER_LABELS)

    async def click_skip_later(self) -> bool:
        if await self._click_by_labels(SKIP_LATER_LABELS):
            self.say("[Browser] 已点击跳过引导 (Maybe later / Later / 之后再说 等)")
            return True
        return False

    async def dismiss_post_signup_prompts(self, max_clicks: int = 4) -> int:
        clicked = 0
        for index in range(1, max_clicks + 1):
            await self.sleep(1500)
            if not await self.has_skip_later_prompt():
                self.say(
                    f"[Browser] 引导页 ({index}/{max_clicks}): "
                    f"未找到跳过按钮 (Maybe later/Later/之后再说 等)，停止"
                )
                break
            if not await self.click_skip_later():
                self.say(f"[Browser] 引导页 ({index}/{max_clicks}): 跳过按钮点击失败，停止")
                break
            clicked += 1
            self.say(f"[Browser] 引导页跳过进度: {clicked}/{max_clicks}")
        return clicked

    async def is_registration_complete(self) -> bool:
        state = await self.detect_state()
        if state == "success":
            return True
        url = (self.page.url or "").lower()
        return "cursor.com/dashboard" in url or "cursor.com/settings" in url

    async def open_settings(self) -> None:
        await self._goto_with_retry(CURSOR_SETTINGS_URL, wait_until="domcontentloaded", timeout=60_000)
        await self.wait_for_cloudflare(30_000)
