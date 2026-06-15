from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from playwright.async_api import Page

from .captcha_auto import (
    _captcha_mode,
    _capsolver_key,
    CAPTCHA_MAX_CLICK_ATTEMPTS,
    CAPTCHA_RETRY_INTERVAL_SEC,
    captcha_strategy_summary,
    security_challenge_visible,
    should_use_capsolver,
    try_auto_clear_security_challenge,
)
from .utils import log, resolve_path, safe_filename


SMS_CODE_CALLBACK = Callable[[], Awaitable[str]]


def _is_chatgpt_root_url(url: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    path = parsed.path or "/"
    return host == "chatgpt.com" and path == "/"


AUTH_ROUTE_ERROR_HINTS = (
    "route error",
    "invalid content type",
    "400 invalid content type",
    "糟糕，出错了",
    "oops, an error occurred",
    "something went wrong",
)

AUTH_RETRY_TEXTS = ["重试", "再试一次", "Retry", "Try again"]


class FreeBrowserFlow:
    """Browser flow helpers for free/email/phone registration and OAuth steps."""

    def __init__(self, page: Page, prefix: str) -> None:
        self.page = page
        self.prefix = prefix

    async def sleep(self, ms: int) -> None:
        await self.page.wait_for_timeout(ms)

    def say(self, message: str) -> None:
        log(f"{self.prefix} {message}")

    async def screenshot(self, filename: str) -> None:
        out = resolve_path("output/free_register/debug")
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{int(time.time())}_{safe_filename(self.prefix)[-80:]}_{filename}"
        await self.page.screenshot(path=str(path), full_page=True)
        self.say(f"[Browser] screenshot: {path}")

    async def goto_chatgpt_entry(self, timeout_ms: int = 60_000) -> None:
        urls = [
            "https://chatgpt.com",
            "https://chatgpt.com/",
            "https://chat.openai.com/",
        ]
        last_error: Exception | None = None
        for idx, url in enumerate(urls, start=1):
            try:
                self.say(f"[Browser] open: {url}")
                await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self.say(f"[Browser] open failed ({idx}/{len(urls)}): {str(exc)[:180]}")
                if idx < len(urls):
                    await self.sleep(1500)
        if last_error:
            raise last_error
        raise RuntimeError("cannot open ChatGPT entry")

    async def _security_challenge_visible(self) -> tuple[bool, str]:
        return await security_challenge_visible(self.page)

    async def auth_route_error_reason(self) -> str:
        try:
            text = await self.page.evaluate("() => document.body?.innerText || ''")
        except Exception:
            return ""
        body = str(text or "")
        haystack = f"{self.page.url or ''} {body}".lower()
        for hint in AUTH_ROUTE_ERROR_HINTS:
            if hint.lower() in haystack:
                if hint.lower() in {"something went wrong", "oops, an error occurred"}:
                    retry_hint = any(k in body.lower() for k in ("retry", "try again", "重试", "再试一次"))
                    if not retry_hint:
                        continue
                return hint
        return ""

    async def recover_auth_route_error(
        self,
        *,
        max_attempts: int = 2,
        wait_ms: int = 12_000,
        raise_on_failure: bool = True,
    ) -> bool:
        recovered = False
        last_reason = ""
        for attempt in range(1, max_attempts + 1):
            reason = await self.auth_route_error_reason()
            if not reason:
                return recovered
            recovered = True
            last_reason = reason
            self.say(f"[Browser] 检测到认证错误页 ({reason})，点击重试 ({attempt}/{max_attempts})")
            clicked = False
            try:
                clicked = await self._find_clickable(AUTH_RETRY_TEXTS)
            except Exception:
                clicked = False
            if not clicked:
                self.say("[Browser] 未找到重试按钮，尝试刷新当前认证页")
                try:
                    await self.page.reload(wait_until="domcontentloaded", timeout=20_000)
                except Exception as exc:
                    self.say(f"[Browser] 刷新认证页失败: {exc.__class__.__name__}: {str(exc)[:120]}")

            deadline = time.monotonic() + wait_ms / 1000
            while time.monotonic() < deadline:
                await self.sleep(700)
                if not await self.auth_route_error_reason():
                    self.say("[Browser] 认证错误页重试后已恢复")
                    return True

        if raise_on_failure:
            raise RuntimeError(f"认证错误页重试后仍未恢复: {last_reason or self.page.url}")
        return recovered

    async def wait_for_cloudflare(self, timeout_ms: int = 60_000) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        last_log = 0.0
        capsolver_tried = False
        strategy_logged = False
        click_attempts = 0
        last_click_at = 0.0
        while time.monotonic() < deadline:
            if await self.recover_auth_route_error(max_attempts=2, wait_ms=10_000):
                last_log = 0.0
                continue
            visible, reason = await self._security_challenge_visible()
            if not visible:
                return
            if not strategy_logged:
                self.say(f"[Captcha] 安全验证策略: {captcha_strategy_summary()}")
                strategy_logged = True
            now = time.monotonic()
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
                    await self.sleep(1200)
                    if not (await self._security_challenge_visible())[0]:
                        self.say("[Browser] 安全验证已自动通过")
                        return
                elif not capsolver_tried and should_use_capsolver() and _capsolver_key():
                    capsolver_tried = True
            now = time.monotonic()
            if now - last_log >= 8:
                self.say(
                    f"[Browser] 等待安全验证通过 ({reason})..."
                    f" Captcha {click_attempts}/{CAPTCHA_MAX_CLICK_ATTEMPTS}"
                )
                last_log = now
            await self.sleep(800)

        visible, reason = await self._security_challenge_visible()
        if not visible:
            return

        if await try_auto_clear_security_challenge(self.page, self.say):
            await self.sleep(2500)
            if not (await self._security_challenge_visible())[0]:
                self.say("[Browser] 安全验证已自动通过")
                return

        if _captcha_mode() == "api":
            raise RuntimeError(f"安全验证自动求解未通过: {reason or self.page.url}")

        self.say(f"[Browser] 安全验证仍未通过 ({reason})，请在浏览器窗口手动完成验证")
        manual_deadline = time.monotonic() + 180
        while time.monotonic() < manual_deadline:
            visible, reason = await self._security_challenge_visible()
            if not visible:
                self.say("[Browser] 安全验证已通过")
                return
            await self.sleep(2000)

        raise RuntimeError(f"安全验证超时: {reason or self.page.url}")

    async def wait_for_text_on_page(self, text: str | list[str], timeout_ms: int = 30_000) -> None:
        candidates = text if isinstance(text, list) else [text]
        lower_candidates = [c.lower() for c in candidates]
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                body = await self.page.evaluate("() => (document.body?.innerText || '').toLowerCase()")
                if any(c in body for c in lower_candidates):
                    return
            except Exception:
                pass
            await self.sleep(1000)
        raise RuntimeError(f"wait text timeout: {'/'.join(candidates)}")

    async def _find_email_input(self):
        selector = (
            'input[type="email"], input[name*="email" i], input[id*="email" i], '
            'input[name="username"], input[autocomplete="username"], input[inputmode="email"], '
            'input[placeholder*="mail" i], input[placeholder*="邮箱" i], '
            '[role="dialog"] input, dialog input, [aria-modal="true"] input'
        )
        try:
            main_loc = self.page.locator(selector)
            if await main_loc.count() > 0:
                return main_loc.first
        except Exception:
            pass
        for frame in self.page.frames:
            if frame == self.page.main_frame:
                continue
            try:
                loc = frame.locator(selector)
                if await loc.count() > 0:
                    return loc.first
            except Exception:
                continue
        return None

    async def _is_probable_auth_input(self, loc) -> tuple[bool, str]:
        try:
            meta = await loc.evaluate(
                """(el) => {
                    const v = (x) => String(x || '').toLowerCase();
                    const type = v(el.getAttribute('type'));
                    const name = v(el.getAttribute('name'));
                    const id = v(el.getAttribute('id'));
                    const placeholder = v(el.getAttribute('placeholder'));
                    const autocomplete = v(el.getAttribute('autocomplete'));
                    const inputmode = v(el.getAttribute('inputmode'));
                    const aria = v(el.getAttribute('aria-label'));
                    const inDialog = !!el.closest('[role="dialog"], dialog, [aria-modal="true"], [data-radix-dialog-content]');
                    return { type, name, id, placeholder, autocomplete, inputmode, aria, inDialog };
                }"""
            )
        except Exception:
            return False, "meta-unavailable"
        hint_text = " ".join(
            [
                meta.get("name", ""),
                meta.get("id", ""),
                meta.get("placeholder", ""),
                meta.get("autocomplete", ""),
                meta.get("inputmode", ""),
                meta.get("aria", ""),
            ]
        )
        probable = bool(
            meta.get("type") == "email"
            or meta.get("inputmode") == "email"
            or any(k in hint_text for k in ("mail", "email", "user", "账号", "邮箱", "login", "signin", "sign in"))
            or meta.get("inDialog")
        )
        return probable, str(meta)

    async def wait_for_email_input(self, timeout_ms: int = 30_000):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            visible, reason = await self._security_challenge_visible()
            if visible:
                await self.wait_for_cloudflare(min(90_000, int((deadline - time.monotonic()) * 1000)))
            loc = await self._find_email_input()
            if loc is not None:
                ok, meta = await self._is_probable_auth_input(loc)
                if ok:
                    self.say(f"[Browser] auth input matched: {meta}")
                    return loc
            await self.sleep(800)
        return None

    async def _find_clickable(self, candidates: list[str]) -> bool:
        # Prefer Playwright native click first (trusted user-like event).
        for text in candidates:
            if not str(text or "").strip():
                continue
            css_text = str(text).replace("\\", "\\\\").replace('"', '\\"')
            selectors = [
                f'button:has-text("{css_text}")',
                f'a:has-text("{css_text}")',
                f'[role="button"]:has-text("{css_text}")',
                f'input[type="button"][value*="{css_text}"]',
                f'input[type="submit"][value*="{css_text}"]',
            ]
            for selector in selectors:
                try:
                    loc = self.page.locator(selector).first
                    if await loc.count() <= 0:
                        continue
                    await loc.scroll_into_view_if_needed(timeout=1500)
                    await loc.click(timeout=2500)
                    return True
                except Exception:
                    continue
        return bool(
            await self.page.evaluate(
                """(texts) => {
                    const visible = (el) => {
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                    };
                    const wanted = texts.map(t => String(t || '').toLowerCase().trim());
                    for (const el of document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]')) {
                        if (!visible(el)) continue;
                        const label = String(el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '').toLowerCase();
                        if (!wanted.some(w => label.includes(w))) continue;
                        el.scrollIntoView({ block: 'center', inline: 'center' });
                        ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(type => {
                            el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
                        });
                        return true;
                    }
                    return false;
                }""",
                candidates,
            )
        )

    async def wait_for_button_by_text(self, text: str | list[str], timeout_ms: int = 30_000) -> None:
        candidates = text if isinstance(text, list) else [text]
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if await self._find_clickable(candidates):
                return
            await self.sleep(1000)
        raise RuntimeError(f"wait button timeout: {'/'.join(candidates)}")

    async def click_button_by_text(self, text: str | list[str], timeout_ms: int = 10_000) -> None:
        candidates = text if isinstance(text, list) else [text]
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if await self._find_clickable(candidates):
                await self.sleep(700)
                return
            await self.sleep(800)
        raise RuntimeError(f"button not found: {'/'.join(candidates)}")

    async def click_submit_button(self) -> None:
        clicked = await self.page.evaluate(
            """() => {
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                };
                const preferred = ['continue', 'next', 'verify', 'submit', 'sign up', '注册', '继续'];
                const blocked = ['google', 'apple', 'phone', '手机号', '电话', '手机'];
                const nodes = Array.from(document.querySelectorAll('button[type="submit"], button, input[type="submit"]'));
                for (const el of nodes) {
                    if (!visible(el) || el.disabled) continue;
                    const text = String(el.innerText || el.textContent || el.value || '').toLowerCase();
                    if (blocked.some(b => text.includes(b))) continue;
                    if (!preferred.some(p => text.includes(p))) continue;
                    ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(type => {
                        el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
                    });
                    return true;
                }
                for (const el of nodes) {
                    if (!visible(el) || el.disabled) continue;
                    const text = String(el.innerText || el.textContent || el.value || '').toLowerCase();
                    if (blocked.some(b => text.includes(b))) continue;
                    ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(type => {
                        el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
                    });
                    return true;
                }
                return false;
            }"""
        )
        if not clicked:
            await self.page.keyboard.press("Enter")
        await self.sleep(800)

    async def wait_for_url_change(self, current_url: str, timeout_ms: int = 15_000) -> str:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            new_url = self.page.url
            if new_url != current_url:
                return new_url
            await self.sleep(500)
        return self.page.url

    async def wait_until_url_leaves(self, keyword: str, timeout_ms: int = 15_000) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if keyword.lower() not in self.page.url.lower():
                return
            await self.sleep(500)

    async def detect_email_already_registered(self) -> None:
        text = await self.page.evaluate("() => (document.body?.innerText || '')")
        low = text.lower()
        hints = [
            "already have an account",
            "already registered",
            "email already",
            "邮箱已被注册",
            "此邮箱已",
        ]
        if any(h in low for h in hints):
            raise RuntimeError("email already registered")

    async def body_text(self) -> str:
        try:
            return str(await self.page.evaluate("() => document.body?.innerText || ''") or "")
        except Exception:
            return ""

    async def _has_visible_selector(self, selector: str) -> bool:
        try:
            return bool(
                await self.page.evaluate(
                    """(selector) => {
                        const visible = (el) => {
                            const r = el.getBoundingClientRect();
                            const s = getComputedStyle(el);
                            return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                        };
                        return Array.from(document.querySelectorAll(selector)).some(visible);
                    }""",
                    selector,
                )
            )
        except Exception:
            return False

    async def _about_you_visible(self, low_text: str) -> bool:
        url = (self.page.url or "").lower()
        if "about-you" in url or "onboarding/profile" in url:
            return True
        hints = (
            "about you",
            "tell us about you",
            "your name",
            "birthday",
            "birth date",
            "age",
            "关于你",
            "你的姓名",
            "出生",
            "年龄",
        )
        if not any(h in low_text for h in hints):
            return False
        return await self._has_visible_selector(
            'input[name*="name" i], input[placeholder*="name" i], input[id*="name" i], '
            'input[name*="age" i], input[placeholder*="age" i], input[id*="age" i], '
            'input[name*="birth" i], input[placeholder*="birth" i], input[type="date"], '
            'input[placeholder*="姓名"], input[placeholder*="年龄"], input[placeholder*="出生"]'
        )

    async def _phone_required_visible(self, low_text: str) -> bool:
        url = (self.page.url or "").lower()
        if any(k in url for k in ("add-phone", "phone-verification", "contact-verification")):
            return True
        if await self._has_visible_selector(
            'input[name="phoneNumberInput"], input[type="tel"], '
            'input[name*="phone" i], input[placeholder*="phone" i], input[aria-label*="phone" i], '
            'input[placeholder*="手机号"], input[aria-label*="手机号"], input[placeholder*="电话"]'
        ):
            return True
        phone_hints = (
            "verify your phone",
            "phone verification",
            "add a phone",
            "add your phone",
            "enter your phone number",
            "sms code",
            "text message",
            "手机号验证",
            "短信验证码",
            "输入手机号",
            "手机号码",
        )
        return any(h in low_text for h in phone_hints)

    async def has_combined_verification_profile_page(self) -> bool:
        return await self._is_combined_verification_profile_page()

    async def detect_free_email_register_state(self) -> str:
        if await self.recover_auth_route_error(max_attempts=1, wait_ms=5_000, raise_on_failure=False):
            return "recovered"
        visible, _ = await self._security_challenge_visible()
        if visible:
            return "security_challenge"
        text = await self.body_text()
        low = text.lower()
        is_chatgpt_root = _is_chatgpt_root_url(self.page.url or "")
        root_has_register = is_chatgpt_root and (
            any(h in text for h in ("注册", "免费注册", "创建账号", "创建帐户"))
            or any(h in low for h in ("sign up", "create account", "register"))
        )
        root_has_upgrade = is_chatgpt_root and ("升级" in text or "upgrade" in low)
        if root_has_upgrade and not root_has_register:
            return "complete_home"
        if any(
            h in low
            for h in (
                "already have an account",
                "already registered",
                "email already",
                "邮箱已被注册",
                "此邮箱已",
            )
        ):
            return "email_already_registered"
        if await self._phone_required_visible(low):
            return "phone_required"
        if await self._has_visible_selector('input[type="password"]'):
            return "password"
        if await self._about_you_visible(low):
            return "about_you"
        if await self._wait_for_code_stage(timeout_ms=250):
            return "email_code"
        if any(
            h in low
            for h in (
                "使用通行密钥创建账户",
                "通行密钥",
                "create a passkey",
                "create passkey",
                "create an account with a passkey",
                "create account with passkey",
                "passkey",
            )
        ) and any(h in low for h in ("跳过", "稍后", "skip", "not now", "maybe later")):
            return "passkey"
        if any(
            h in low or h in text
            for h in (
                "是什么促使你使用 chatgpt",
                "是什么促使你使用 ChatGPT",
                "什么促使你使用 chatgpt",
                "what brings you to chatgpt",
                "what brings you here",
                "what are you using chatgpt for",
            )
        ) and any(h in low or h in text for h in ("跳过", "skip")):
            return "usage_reason"
        if any(
            h in low
            for h in (
                "全新 chatgpt images 2.0 重磅登场",
                "chatgpt images 2.0",
                "images 2.0",
            )
        ) and any(h in low for h in ("暂不", "以后再说", "not now", "maybe later", "skip")):
            return "images_upsell"
        email_input = await self._find_email_input()
        if email_input is not None:
            ok, _ = await self._is_probable_auth_input(email_input)
            if ok:
                return "email"
        if root_has_register:
            return "entry"
        if root_has_upgrade:
            return "complete_home"
        ready_primary = any(
            h in low
            for h in (
                "you're all set",
                "you are all set",
                "ready to go",
                "get started",
                "你已准备就绪",
                "您已准备就绪",
                "准备就绪",
            )
        )
        ready_secondary = any(h in low for h in ("chatgpt can make mistakes", "chatgpt 可能会出错", "请勿分享敏感信息"))
        ready_action = any(h in low for h in ("continue", "get started", "start", "继续", "开始"))
        if ready_primary or (ready_secondary and ready_action):
            return "ready_continue"
        if any(
            h in low
            for h in (
                "sign up",
                "sign up for free",
                "continue with email",
                "use email",
                "注册",
                "继续使用邮箱",
                "邮箱",
            )
        ):
            return "entry"
        return "unknown"

    async def click_ready_continue(self) -> bool:
        try:
            await self.click_button_by_text(
                [
                    "Continue",
                    "Get started",
                    "Start using ChatGPT",
                    "继续",
                    "开始",
                    "开始使用 ChatGPT",
                ],
                timeout_ms=8_000,
            )
            return True
        except Exception:
            return False

    async def click_usage_reason_skip(self) -> bool:
        try:
            await self.click_button_by_text(
                [
                    "跳过",
                    "Skip",
                    "稍后",
                    "Not now",
                ],
                timeout_ms=8_000,
            )
            return True
        except Exception:
            return False

    async def click_images_upsell_dismiss(self) -> bool:
        try:
            await self.click_button_by_text(
                [
                    "暂不",
                    "以后再说",
                    "Not now",
                    "Maybe later",
                    "Skip",
                ],
                timeout_ms=8_000,
            )
            return True
        except Exception:
            return False

    async def _dismiss_chatgpt_composer_blockers(self) -> None:
        for labels in (
            ["暂不", "以后再说", "Not now", "Maybe later", "Skip", "Close", "关闭", "Dismiss", "取消", "Later", "後で"],
            ["New chat", "新聊天", "新規チャット", "新しいチャット"],
            ["Start chatting", "开始聊天", "チャットを始める"],
        ):
            try:
                await self.click_button_by_text(labels, timeout_ms=700)
                await self.sleep(350)
            except Exception:
                continue

    async def send_chatgpt_smoke_message(self, prompt: str = "你好", timeout_ms: int = 90_000) -> str:
        await self.wait_for_cloudflare(30_000)
        await self._dismiss_chatgpt_composer_blockers()
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:
            pass
        before_count = 0
        try:
            before_count = int(
                await self.page.evaluate(
                    """() => document.querySelectorAll('[data-message-author-role="assistant"]').length"""
                )
            )
        except Exception:
            before_count = 0

        selectors = (
            'div#prompt-textarea.ProseMirror[contenteditable="true"]',
            'div#prompt-textarea[contenteditable="true"]',
            '#prompt-textarea',
            '[data-testid="prompt-textarea"]',
            'textarea[placeholder*="Message" i]',
            'textarea[placeholder*="Ask" i]',
            'textarea[placeholder*="消息"]',
            'textarea[placeholder*="发送"]',
            'textarea[placeholder*="メッセージ"]',
            'textarea[placeholder*="質問"]',
            'textarea[data-testid]',
            'div[contenteditable="true"][role="textbox"]',
            'div[contenteditable="true"][data-placeholder]',
            '[contenteditable="true"][data-testid*="prompt" i]',
            'form [contenteditable="true"]',
            'main [contenteditable="true"]',
            'textarea[placeholder]',
        )

        async def try_dom_fill_composer() -> bool:
            try:
                result = await self.page.evaluate(
                    """(value) => {
                        // chatgptComposerSmokeFill
                        const visible = (el) => {
                            if (!el) return false;
                            const rect = el.getBoundingClientRect();
                            const style = getComputedStyle(el);
                            return rect.width > 0 &&
                                rect.height > 0 &&
                                style.display !== 'none' &&
                                style.visibility !== 'hidden' &&
                                Number(style.opacity || '1') > 0.01;
                        };
                        const setTextarea = (el, text) => {
                            const proto = Object.getPrototypeOf(el);
                            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                            if (desc && typeof desc.set === 'function') {
                                desc.set.call(el, text);
                            } else {
                                el.value = text;
                            }
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        };
                        const dispatchTextInput = (el, text) => {
                            el.focus();
                            const selection = window.getSelection();
                            if (selection) {
                                const range = document.createRange();
                                range.selectNodeContents(el);
                                range.collapse(false);
                                selection.removeAllRanges();
                                selection.addRange(range);
                            }
                            let inserted = false;
                            try {
                                inserted = document.execCommand('insertText', false, text);
                            } catch {}
                            if (!inserted || !String(el.innerText || el.textContent || '').includes(text)) {
                                el.textContent = '';
                                const p = document.createElement('p');
                                p.textContent = text;
                                el.appendChild(p);
                            }
                            el.dispatchEvent(new InputEvent('beforeinput', {
                                bubbles: true,
                                cancelable: true,
                                inputType: 'insertText',
                                data: text,
                            }));
                            el.dispatchEvent(new InputEvent('input', {
                                bubbles: true,
                                inputType: 'insertText',
                                data: text,
                            }));
                        };

                        const editors = Array.from(document.querySelectorAll([
                            'div#prompt-textarea.ProseMirror[contenteditable="true"]',
                            '#prompt-textarea[contenteditable="true"]',
                            '.ProseMirror[contenteditable="true"]',
                            '[data-composer-surface="true"] [contenteditable="true"]',
                            'div[contenteditable="true"][data-placeholder]',
                            'div[contenteditable="true"][role="textbox"]',
                            'main [contenteditable="true"]'
                        ].join(','))).filter(visible);
                        if (editors.length) {
                            dispatchTextInput(editors[0], String(value));
                            const fallback = document.querySelector('textarea[name="prompt-textarea"], textarea.wcDTda_fallbackTextarea');
                            if (fallback) setTextarea(fallback, String(value));
                            return { filled: true, mode: 'contenteditable', text: editors[0].innerText || '' };
                        }

                        const textareas = Array.from(document.querySelectorAll([
                            'textarea[name="prompt-textarea"]',
                            'textarea[placeholder]',
                            'textarea[data-testid]'
                        ].join(','))).filter(visible);
                        if (textareas.length) {
                            setTextarea(textareas[0], String(value));
                            textareas[0].focus();
                            return { filled: true, mode: 'textarea', text: textareas[0].value || '' };
                        }
                        return { filled: false, mode: 'not-found', text: '' };
                    }""",
                    prompt,
                )
                return bool(isinstance(result, dict) and result.get("filled"))
            except Exception:
                return False

        async def try_prepare_composer() -> None:
            await self._dismiss_chatgpt_composer_blockers()

        async def try_fill_locator(loc, prompt_text: str) -> bool:
            try:
                if await loc.count() <= 0:
                    return False
                try:
                    await loc.wait_for(state="visible", timeout=2_500)
                except Exception:
                    if not await loc.is_visible(timeout=800):
                        return False
                await loc.click(timeout=2_000)
                tag_name = ""
                try:
                    tag_name = str(await loc.evaluate("(el) => el.tagName.toLowerCase()"))
                except Exception:
                    tag_name = ""
                if tag_name == "textarea":
                    await loc.fill(prompt_text, timeout=4_000)
                else:
                    try:
                        await loc.fill(prompt_text, timeout=4_000)
                    except Exception:
                        await loc.evaluate(
                            """(el, value) => {
                                el.focus();
                                el.innerText = String(value);
                                el.dispatchEvent(new InputEvent('input', {
                                    bubbles: true,
                                    inputType: 'insertText',
                                    data: String(value),
                                }));
                            }""",
                            prompt_text,
                        )
                return True
            except Exception:
                try:
                    await loc.click(timeout=1_500)
                    await loc.press_sequentially(prompt_text, delay=25)
                    return True
                except Exception:
                    return False

        filled = False
        navigated_home = False
        fill_deadline = time.monotonic() + min(60, max(12, timeout_ms / 1000 / 2))
        while time.monotonic() < fill_deadline and not filled:
            try:
                test_id_loc = self.page.get_by_test_id("prompt-textarea").first
                filled = await try_fill_locator(test_id_loc, prompt)
            except Exception:
                filled = False
            if filled:
                break
            for selector in selectors:
                try:
                    loc = self.page.locator(selector).first
                    if await try_fill_locator(loc, prompt):
                        filled = True
                        break
                except Exception:
                    continue
            if not filled:
                filled = await try_dom_fill_composer()
            if filled:
                break
            await try_prepare_composer()
            if not navigated_home and "chatgpt.com" in (self.page.url or "").lower() and time.monotonic() + 20 < fill_deadline:
                navigated_home = True
                try:
                    await self.page.goto("https://chatgpt.com/?model=auto", wait_until="domcontentloaded", timeout=30_000)
                    await self.wait_for_cloudflare(30_000)
                except Exception:
                    pass
            await self.sleep(1000)

        if not filled:
            raise RuntimeError(f"未找到 ChatGPT 输入框，无法发送探测消息: {self.page.url}")

        await self.sleep(300)
        clicked = False
        for selector in (
            '[data-testid="send-button"]',
            '[data-testid="composer-submit-button"]',
            'button[aria-label*="Send" i]',
            'button[aria-label*="发送" i]',
            'button:has-text("Send")',
            'button:has-text("发送")',
        ):
            try:
                btn = self.page.locator(selector).first
                if await btn.count() <= 0:
                    continue
                if not await btn.is_visible(timeout=600):
                    continue
                await btn.click(timeout=2000)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            await self.page.keyboard.press("Enter")

        deadline = time.monotonic() + timeout_ms / 1000
        last_reply = ""
        while time.monotonic() < deadline:
            try:
                result = await self.page.evaluate(
                    """(beforeCount) => {
                        const nodes = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'));
                        const latest = nodes.length ? String(nodes[nodes.length - 1].innerText || '').trim() : '';
                        const stopVisible = Array.from(document.querySelectorAll('button,[role="button"]')).some((el) => {
                            const text = String(el.innerText || el.textContent || el.getAttribute('aria-label') || '').toLowerCase();
                            const rect = el.getBoundingClientRect();
                            const style = getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0 &&
                                style.display !== 'none' &&
                                style.visibility !== 'hidden' &&
                                (text.includes('stop') || text.includes('停止'));
                        });
                        return { count: nodes.length, latest, stopVisible };
                    }""",
                    before_count,
                )
                if isinstance(result, dict):
                    latest = str(result.get("latest") or "").strip()
                    if latest:
                        last_reply = latest
                    if int(result.get("count") or 0) > before_count and latest and not bool(result.get("stopVisible")):
                        return latest
            except Exception:
                pass
            await self.sleep(1000)

        if last_reply:
            return last_reply
        raise RuntimeError("等待 ChatGPT 回复超时")

    async def click_passkey_skip(self) -> bool:
        try:
            await self.click_button_by_text(
                [
                    "Skip",
                    "Not now",
                    "Maybe later",
                    "Continue without passkey",
                    "跳过",
                    "稍后",
                    "暂不",
                    "以后再说",
                ],
                timeout_ms=8_000,
            )
            return True
        except Exception:
            return False

    async def click_signup_email_entry(self) -> bool:
        try:
            await self.click_button_by_text(["Reject non-essential", "拒绝非必需", "Accept all", "全部接受"], 2_000)
        except Exception:
            pass
        if await self._find_email_input() is not None:
            return True
        clicked = False
        try:
            await self.click_button_by_text(["Sign up", "Sign up for free", "免费注册", "注册"], 4_000)
            clicked = True
        except Exception:
            pass
        try:
            await self.click_button_by_text(["Continue with email", "Use email", "Email", "继续使用邮箱", "邮箱"], 3_000)
            clicked = True
        except Exception:
            pass
        return clicked or (await self._find_email_input() is not None)

    async def _click_email_submit_near_input(self, email_input) -> bool:
        try:
            result = await asyncio.wait_for(
                email_input.evaluate(
                    """(input) => {
                        const visible = (el) => {
                            if (!el) return false;
                            const rect = el.getBoundingClientRect();
                            const style = getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0
                                && style.display !== 'none'
                                && style.visibility !== 'hidden';
                        };
                        const text = (el) => String(
                            el?.innerText || el?.textContent || el?.value
                            || el?.getAttribute?.('aria-label')
                            || el?.getAttribute?.('title')
                            || el?.getAttribute?.('data-testid')
                            || ''
                        ).replace(/\\s+/g, ' ').trim().toLowerCase();
                        const blocked = (el) => /google|apple|microsoft|github|sso|oauth|social|provider|phone|手机|电话|手机号/.test(
                            `${text(el)} ${el?.outerHTML || ''}`.toLowerCase()
                        );
                        const wanted = (el) => {
                            const value = text(el);
                            return /^(continue|next|submit|log in|sign in|sign up|create|verify|继续|下一步|登录|注册|提交|验证)$/.test(value)
                                || /continue|next|submit|login|sign in|sign up|verify|继续|下一步|登录|注册|提交|验证/.test(value);
                        };
                        const activate = (target) => {
                            target.scrollIntoView({ block: 'center', inline: 'nearest' });
                            target.focus?.();
                            target.click();
                            ['pointerdown','mousedown','pointerup','mouseup','click'].forEach((type) => {
                                target.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
                            });
                            const form = target.closest?.('form') || input.closest?.('form');
                            if (form && typeof form.requestSubmit === 'function') {
                                setTimeout(() => {
                                    try { form.requestSubmit(target instanceof HTMLButtonElement ? target : undefined); } catch {}
                                }, 50);
                            }
                        };
                        const form = input.closest('form');
                        const scopes = [
                            form,
                            input.closest('[role="dialog"], dialog, [aria-modal="true"], [data-radix-dialog-content]'),
                            input.closest('section'),
                            input.closest('main'),
                            input.closest('[role="main"]'),
                            document
                        ].filter(Boolean);
                        for (const scope of scopes) {
                            const buttons = Array.from(scope.querySelectorAll('button, input[type="submit"], [role="button"]'))
                                .filter((el) => visible(el) && !el.disabled && !blocked(el));
                            const preferred = buttons.find(wanted);
                            if (preferred) {
                                activate(preferred);
                                return { clicked: true, mode: 'preferred', label: text(preferred) };
                            }
                            if (scope === form && buttons.length === 1) {
                                activate(buttons[0]);
                                return { clicked: true, mode: 'single-form-button', label: text(buttons[0]) };
                            }
                        }
                        input.focus();
                        input.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Enter', code: 'Enter' }));
                        input.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'Enter', code: 'Enter' }));
                        if (form && typeof form.requestSubmit === 'function') {
                            try {
                                form.requestSubmit();
                                return { clicked: true, mode: 'form-request-submit', label: '' };
                            } catch {}
                        }
                        return { clicked: false, mode: 'not-found', label: '' };
                    }"""
                ),
                timeout=2.5,
            )
            if isinstance(result, dict) and result.get("clicked"):
                self.say(f"[Browser] clicked email confirm: {result.get('mode')} {result.get('label') or ''}".rstrip())
                try:
                    await self.page.keyboard.press("Enter")
                except Exception:
                    pass
                return True
        except Exception as exc:
            self.say(f"[Browser] email confirm click failed: {exc.__class__.__name__}: {str(exc)[:120]}")
        try:
            await self.page.keyboard.press("Enter")
            self.say("[Browser] pressed Enter for email confirm")
            return True
        except Exception:
            return False

    async def wait_for_post_email_state(self, timeout_ms: int = 8_000) -> str:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            state = await self.detect_free_email_register_state()
            if state in {"email_code", "password", "about_you", "phone_required", "email_already_registered"}:
                return state
            if state == "recovered":
                await self.sleep(500)
                continue
            await self.sleep(500)
        return await self.detect_free_email_register_state()

    async def submit_email_if_present(self, email: str) -> bool:
        email_input = await self.wait_for_email_input(2_000)
        if email_input is None:
            return False
        before = self.page.url
        for attempt in range(1, 4):
            refreshed = await self.wait_for_email_input(1_500)
            if refreshed is not None:
                email_input = refreshed
            try:
                current_value = await email_input.input_value(timeout=800)
            except Exception:
                current_value = ""
            if str(current_value or "").strip().lower() != email.strip().lower():
                await email_input.click(click_count=3)
                await email_input.fill("")
                await email_input.type(email, delay=30)
                await self.sleep(500)
            clicked = await self._click_email_submit_near_input(email_input)
            if not clicked:
                self.say(f"[Browser] email confirm button not found on attempt {attempt}")
            next_state = await self.wait_for_post_email_state(timeout_ms=8_000)
            if next_state == "email_already_registered":
                await self.detect_email_already_registered()
                raise RuntimeError("email already registered")
            if next_state in {"email_code", "password", "about_you", "phone_required"}:
                self.say(f"[Browser] email submit advanced to {next_state} on attempt {attempt}")
                break
            await self.detect_email_already_registered()
            if await self.recover_auth_route_error(max_attempts=2, wait_ms=8_000):
                continue
            self.say(f"[Browser] email submit still on {next_state} (attempt {attempt})")
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=1_500)
            except Exception:
                pass
            await self.sleep(800)
        await self.wait_for_url_change(before, timeout_ms=3_000)
        await self.wait_for_cloudflare(60_000)
        await self.detect_email_already_registered()
        return True

    async def navigate_to_signup(self) -> None:
        await self.goto_chatgpt_entry(timeout_ms=60_000)
        await self.wait_for_cloudflare()
        await self.wait_for_button_by_text(["Sign up", "Sign up for free", "免费注册", "注册"], 30_000)
        await self.click_button_by_text(["Sign up", "Sign up for free", "免费注册", "注册"], 12_000)
        await self.sleep(1200)
        await self.click_button_by_text(["Continue with phone", "phone number", "手机", "电话"], 12_000)
        await self.page.locator('input[name="phoneNumberInput"], input[type="tel"]').first.wait_for(timeout=15_000)

    async def navigate_to_signup_email(self, email: str) -> None:
        await self.goto_chatgpt_entry(timeout_ms=60_000)
        await self.wait_for_cloudflare()
        # Best-effort dismiss cookie banners that can intercept clicks.
        try:
            await self.click_button_by_text(["Reject non-essential", "拒绝非必需", "Accept all", "全部接受"], 3_000)
        except Exception:
            pass
        email_input = await self.wait_for_email_input(6_000)
        if email_input is not None:
            self.say("[Browser] direct email auth page detected, skipping Sign up button search")
        else:
            await self.wait_for_button_by_text(["Sign up", "Sign up for free", "免费注册", "注册"], 30_000)
            for _ in range(3):
                await self.click_button_by_text(["Sign up", "Sign up for free", "免费注册", "注册"], 12_000)
                await self.sleep(1200)
                try:
                    await self.click_button_by_text(
                        ["Continue with email", "Use email", "Email", "继续使用邮箱", "邮箱"],
                        timeout_ms=3_000,
                    )
                except Exception:
                    pass
                email_input = await self.wait_for_email_input(8_000)
                if email_input is not None:
                    break
        if email_input is None:
            raise RuntimeError("email input not found after signup click")
        await email_input.click(click_count=3)
        await email_input.fill("")
        await email_input.type(email, delay=30)
        await self.sleep(500)
        before = self.page.url
        for attempt in range(1, 4):
            submitted = False
            # Re-acquire the email input each attempt to avoid stale execution context
            # after in-dialog navigation transitions.
            refreshed = await self.wait_for_email_input(1_500)
            if refreshed is not None:
                email_input = refreshed
                try:
                    current_value = await email_input.input_value(timeout=800)
                except Exception:
                    current_value = ""
                if str(current_value or "").strip().lower() != email.strip().lower():
                    try:
                        await email_input.click(click_count=3)
                        await email_input.fill("")
                        await email_input.type(email, delay=30)
                    except Exception:
                        pass
            try:
                clicked_form_submit = await asyncio.wait_for(
                    email_input.evaluate(
                        """(el) => {
                            const block = (txt) => ['google', 'apple', 'phone', '手机', '电话', '手机号'].some(k => txt.includes(k));
                            const form = el.closest('form');
                            if (form) {
                                const btn = form.querySelector('button[type="submit"], input[type="submit"], button');
                                if (btn) {
                                    const text = String(btn.innerText || btn.textContent || btn.value || '').toLowerCase();
                                    if (!block(text)) {
                                        btn.click();
                                        return true;
                                    }
                                }
                            }
                            const dialog = el.closest('[role="dialog"], dialog, [aria-modal="true"], [data-radix-dialog-content]');
                            if (dialog) {
                                const btns = Array.from(dialog.querySelectorAll('button[type="submit"], button, input[type="submit"]'));
                                for (const btn of btns) {
                                    const text = String(btn.innerText || btn.textContent || btn.value || '').toLowerCase();
                                    if (block(text)) continue;
                                    if (['continue', 'next', 'submit', '继续', '下一步', '提交', '注册'].some(k => text.includes(k))) {
                                        btn.click();
                                        return true;
                                    }
                                }
                            }
                            return false;
                        }"""
                    ),
                    timeout=2.5,
                )
                submitted = bool(clicked_form_submit)
            except Exception:
                pass
            if not submitted:
                try:
                    await self.page.keyboard.press("Enter")
                    submitted = True
                except Exception:
                    pass
            if await self._wait_for_code_stage(timeout_ms=6_000):
                self.say(f"[Browser] email submit advanced to code stage on attempt {attempt}")
                break
            self.say(f"[Browser] email submit did not reach code stage (attempt {attempt})")
            if await self.recover_auth_route_error(max_attempts=2, wait_ms=8_000):
                continue
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=1_500)
            except Exception:
                pass
            await self.sleep(800)
        await self.wait_for_url_change(before, timeout_ms=10_000)
        await self.wait_for_cloudflare(90_000)
        await self.detect_email_already_registered()

    async def click_resend_code(self) -> bool:
        try:
            await self.click_button_by_text(
                [
                    "Send code",
                    "Send verification code",
                    "Send email",
                    "Resend",
                    "Send again",
                    "发送验证码",
                    "发送验证代码",
                    "发送代码",
                    "发送邮件",
                    "重新发送",
                    "再次发送",
                    "重发",
                ],
                6000,
            )
            return True
        except Exception:
            return False

    async def _fill_verification_code_by_dom(self, code: str) -> bool:
        digits = re.sub(r"\D+", "", str(code or ""))[:8]
        if not digits:
            return False
        try:
            result = await self.page.evaluate(
                """(code) => {
                    const visible = (el) => {
                        if (!el || el.disabled || el.readOnly) return false;
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return rect.width > 2 && rect.height > 2
                            && style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && Number(style.opacity || '1') > 0.01;
                    };
                    const textOf = (el) => String([
                        el.getAttribute('name'),
                        el.getAttribute('id'),
                        el.getAttribute('autocomplete'),
                        el.getAttribute('inputmode'),
                        el.getAttribute('aria-label'),
                        el.getAttribute('placeholder'),
                        el.getAttribute('data-testid'),
                        el.getAttribute('type')
                    ].filter(Boolean).join(' ')).toLowerCase();
                    const pageText = String(document.body?.innerText || '').toLowerCase();
                    const pageLooksCode = /verification code|enter code|check your email|one-time|otp|验证码|验证代码|输入代码|输入验证码/.test(pageText);
                    const blocked = (el) => /email|mail|邮箱|password|pass|密码|name|姓名|age|年龄|birth|生日|phone|tel|手机|电话/.test(textOf(el));
                    const isCodeLike = (el) => {
                        const hint = textOf(el);
                        const maxLength = Number(el.getAttribute('maxlength') || 0);
                        return /code|otp|one-time|verification|验证码|验证代码|代码/.test(hint)
                            || el.getAttribute('autocomplete') === 'one-time-code'
                            || el.getAttribute('inputmode') === 'numeric'
                            || el.getAttribute('type') === 'tel'
                            || (pageLooksCode && maxLength > 0 && maxLength <= 8 && !blocked(el));
                    };
                    const setValue = (el, value) => {
                        el.focus?.();
                        const proto = Object.getPrototypeOf(el);
                        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                        if (desc && typeof desc.set === 'function') {
                            desc.set.call(el, '');
                            desc.set.call(el, String(value));
                        } else {
                            el.value = String(value);
                        }
                        for (const type of ['beforeinput', 'input', 'change', 'keyup']) {
                            try { el.dispatchEvent(new Event(type, { bubbles: true, cancelable: true })); } catch {}
                        }
                    };
                    const all = Array.from(document.querySelectorAll('input, textarea'))
                        .filter((el) => visible(el) && !blocked(el));
                    let candidates = all.filter(isCodeLike);
                    if (!candidates.length && pageLooksCode) {
                        candidates = all.filter((el) => {
                            const type = String(el.getAttribute('type') || 'text').toLowerCase();
                            return ['text', 'tel', 'number', ''].includes(type);
                        });
                    }
                    if (!candidates.length) {
                        return { filled: false, mode: 'not-found', count: 0 };
                    }

                    const single = candidates.find((el) => {
                        const maxLength = Number(el.getAttribute('maxlength') || 0);
                        return maxLength >= code.length || maxLength === 0 || el.tagName.toLowerCase() === 'textarea';
                    });
                    if (single && candidates.length < 6) {
                        setValue(single, code);
                        return { filled: true, mode: 'single', count: candidates.length };
                    }

                    const boxes = candidates.slice(0, code.length);
                    if (boxes.length >= Math.min(6, code.length)) {
                        boxes.forEach((el, idx) => setValue(el, code[idx] || ''));
                        return { filled: true, mode: 'split', count: boxes.length };
                    }

                    setValue(candidates[0], code);
                    return { filled: true, mode: 'fallback-single', count: candidates.length };
                }""",
                digits,
            )
            if isinstance(result, dict) and result.get("filled"):
                self.say(f"[Browser] verification code filled by DOM: {result.get('mode')} count={result.get('count')}")
                return True
        except Exception as exc:
            self.say(f"[Browser] DOM 填写验证码失败: {exc.__class__.__name__}: {str(exc)[:120]}")
        return False

    async def enter_sms_code(self, code: str) -> None:
        await self.wait_for_cloudflare(90_000)
        if await self._fill_verification_code_by_dom(code):
            await self.sleep(800)
            return
        inputs = self.page.locator(
            'input[name*="code" i], input[inputmode="numeric"], input[autocomplete="one-time-code"], input[type="tel"]'
        )
        await inputs.first.wait_for(timeout=20_000)
        count = await inputs.count()
        if count >= 6:
            for i, ch in enumerate(code[:6]):
                box = inputs.nth(i)
                try:
                    await box.fill(ch)
                except Exception:
                    await box.click()
                    await box.type(ch, delay=40)
        else:
            box = inputs.first
            await box.click(click_count=3)
            await box.fill("")
            await box.type(code, delay=60)
        await self.sleep(800)

    async def enter_email_verification_code(self, code: str) -> None:
        await self.enter_sms_code(code)
        before = self.page.url
        await self.click_submit_button()
        await self.wait_for_url_change(before, timeout_ms=15_000)
        await self.wait_for_cloudflare(30_000)
        await self.recover_auth_route_error(max_attempts=2, wait_ms=10_000)

    async def _wait_for_code_stage(self, timeout_ms: int = 20_000) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                if await self.recover_auth_route_error(max_attempts=1, wait_ms=5_000, raise_on_failure=False):
                    continue
            except Exception:
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            probe_timeout = min(1.0, max(0.25, remaining))
            try:
                code_inputs = self.page.locator(
                    'input[name*="code" i], input[autocomplete="one-time-code"], input[inputmode="numeric"]'
                )
                count = await asyncio.wait_for(code_inputs.count(), timeout=probe_timeout)
                if count > 0:
                    return True
            except Exception:
                pass
            try:
                body = await asyncio.wait_for(
                    self.page.evaluate("() => (document.body?.innerText || '').toLowerCase()"),
                    timeout=probe_timeout,
                )
                if any(
                    k in body
                    for k in (
                        "verification code",
                        "enter code",
                        "check your email",
                        "验证码",
                        "输入验证码",
                    )
                ):
                    return True
            except Exception:
                pass
            await self.sleep(250)
        return False

    async def detect_post_email_stage(self, timeout_ms: int = 12_000) -> str:
        """Detect whether the flow is currently at password page or email-code page."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                if await self.page.locator("input[type='password']").count() > 0:
                    return "password"
            except Exception:
                pass
            if await self._wait_for_code_stage(timeout_ms=1200):
                return "email_code"
            await self.sleep(250)
        return "unknown"

    async def fill_password_input(self, selector: str, password: str) -> None:
        box = self.page.locator(selector).first
        await box.wait_for(timeout=15_000)
        await box.click(click_count=3)
        await box.fill("")
        await box.type(password, delay=35)

    async def fill_password_if_shown(self, password: str) -> bool:
        if await self.page.locator("input[type='password']").count() <= 0:
            # Some auth variants show a "continue with password" gate first.
            try:
                await self.click_button_by_text(
                    ["Continue with password", "Use password", "使用密码继续", "密码继续"],
                    timeout_ms=4_000,
                )
                await self.sleep(800)
            except Exception:
                pass
        if await self.page.locator("input[type='password']").count() <= 0:
            return False
        await self.fill_password_input("input[type='password']", password)
        await self.click_submit_button()
        await self.wait_for_cloudflare(30_000)
        return True

    async def _fill_first_visible_input(self, selectors: list[str], value: str) -> bool:
        if not value:
            return False
        try:
            return bool(
                await self.page.evaluate(
                    """(args) => {
                        const selectors = args.selectors || [];
                        const value = String(args.value ?? "");
                        const visible = (el) => {
                            const r = el.getBoundingClientRect();
                            const s = getComputedStyle(el);
                            return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                        };
                        for (const selector of selectors) {
                            const nodes = document.querySelectorAll(selector);
                            for (const el of nodes) {
                                if (!el) continue;
                                if (el.disabled || el.readOnly) continue;
                                if (!visible(el)) continue;
                                try { el.focus(); } catch {}
                                try {
                                    el.value = '';
                                    el.dispatchEvent(new Event('input', { bubbles: true }));
                                    el.value = value;
                                    el.dispatchEvent(new Event('input', { bubbles: true }));
                                    el.dispatchEvent(new Event('change', { bubbles: true }));
                                    return true;
                                } catch {}
                            }
                        }
                        return false;
                    }""",
                    {"selectors": selectors, "value": value},
                )
            )
        except Exception:
            return False

    async def _fill_locator_with_verify(self, locator, value: str) -> bool:
        if not value:
            return False
        val = str(value)
        try:
            await locator.wait_for(state="visible", timeout=700)
        except Exception:
            return False
        try:
            await locator.click(timeout=700)
        except Exception:
            pass
        try:
            await locator.fill("", timeout=700)
        except Exception:
            pass
        try:
            await locator.type(val, delay=20, timeout=2000)
        except Exception:
            try:
                await locator.fill(val, timeout=1000)
            except Exception:
                return False
        await self.sleep(60)
        try:
            cur = (await locator.input_value()).strip()
            if cur:
                if cur == val or val in cur or cur in val:
                    return True
        except Exception:
            pass
        try:
            cur = await locator.evaluate(
                """(el, v) => {
                    const proto = Object.getPrototypeOf(el);
                    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                    if (desc && typeof desc.set === 'function') {
                        desc.set.call(el, '');
                        desc.set.call(el, String(v));
                    } else {
                        el.value = String(v);
                    }
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return String(el.value || '');
                }""",
                val,
            )
            return bool((cur or "").strip())
        except Exception:
            return False

    async def _fill_profile_field(
        self,
        *,
        value: str,
        label_keywords: list[str],
        selectors: list[str],
        exclude_code_input: bool = True,
    ) -> bool:
        if not value:
            return False
        # Prefer inputs near explicit labels.
        for keyword in label_keywords:
            kw = keyword.strip()
            if not kw:
                continue
            xpath = (
                "xpath=(//*[self::label or self::span or self::div or self::p]"
                f"[contains(normalize-space(.), \"{kw}\")])[1]/following::input[1]"
            )
            loc = self.page.locator(xpath).first
            if await self._fill_locator_with_verify(loc, value):
                return True
        # Fallback to selector list.
        for selector in selectors:
            locs = self.page.locator(selector)
            try:
                count = await locs.count()
            except Exception:
                continue
            for idx in range(min(count, 6)):
                loc = locs.nth(idx)
                if exclude_code_input:
                    try:
                        is_code_like = bool(
                            await loc.evaluate(
                                """(el) => {
                                    const t = String(
                                        (el.getAttribute('name') || '') + ' ' +
                                        (el.getAttribute('id') || '') + ' ' +
                                        (el.getAttribute('autocomplete') || '') + ' ' +
                                        (el.getAttribute('aria-label') || '')
                                    ).toLowerCase();
                                    return t.includes('code') || t.includes('otp') || t.includes('one-time');
                                }"""
                            )
                        )
                    except Exception:
                        is_code_like = False
                    if is_code_like:
                        continue
                if await self._fill_locator_with_verify(loc, value):
                    return True
        return False

    async def _has_profile_validation_errors(self) -> bool:
        try:
            body = (await self.page.evaluate("() => (document.body?.innerText || '').toLowerCase()"))[:4000]
        except Exception:
            return False
        return any(
            msg in body
            for msg in (
                "请输入姓名",
                "请输入有效年龄",
                "please enter your name",
                "enter a valid age",
                "name is required",
                "age is required",
            )
        )

    async def _is_combined_verification_profile_page(self) -> bool:
        try:
            return bool(
                await self.page.evaluate(
                    """() => {
                        const hasCode = !!document.querySelector(
                            'input[name*="code" i], input[autocomplete="one-time-code"], input[inputmode="numeric"]'
                        );
                        const hasNameOrAge = !!document.querySelector(
                            'input[name*="name" i], input[placeholder*="name" i], input[placeholder*="姓名"], '
                            + 'input[name*="age" i], input[placeholder*="age" i], input[placeholder*="年龄"]'
                        );
                        return hasCode && hasNameOrAge;
                    }"""
                )
            )
        except Exception:
            return False

    async def fill_about_you_and_submit(
        self,
        full_name: str,
        age: str,
        birth_date: str,
        tag: str = "[AboutYou]",
        verification_code: str = "",
    ) -> None:
        await self.sleep(200)
        age_text = str(age or "").strip()
        y, m, d = "", "", ""
        if isinstance(birth_date, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", birth_date):
            y, m, d = birth_date.split("-")

        for step in range(1, 5):
            try:
                body = (await self.page.evaluate("() => (document.body?.innerText || '').toLowerCase()"))[:4000]
            except Exception:
                body = ""

            # If no profile-related hints remain, we consider this stage complete.
            if step > 1 and not any(
                k in body
                for k in (
                    "about you",
                    "tell us",
                    "your name",
                    "birthday",
                    "birth",
                    "age",
                    "关于你",
                    "你的姓名",
                    "出生",
                    "年龄",
                )
            ):
                break

            filled_any = False
            if await self._is_combined_verification_profile_page():
                self.say(f"{tag} detected combined verification+profile page")

            # Some variants place verification code and profile fields on the same page.
            code_text = re.sub(r"\D+", "", str(verification_code or ""))[:6]
            if code_text and await self._fill_first_visible_input(
                [
                    'input[name*="code" i]',
                    'input[autocomplete="one-time-code"]',
                    'input[inputmode="numeric"]',
                    'input[aria-label*="验证码"]',
                    'input[placeholder*="验证码"]',
                ],
                code_text,
            ):
                filled_any = True
                self.say(f"{tag} filled verification code")

            # Fill name with label-aware matching and strict verification.
            if await self._fill_profile_field(
                value=full_name,
                label_keywords=["全名", "姓名", "Name", "Full name"],
                selectors=[
                    'input[name*="name" i]',
                    'input[placeholder*="name" i]',
                    'input[id*="name" i]',
                    'input[placeholder*="姓名"]',
                    'input[aria-label*="姓名"]',
                ],
            ):
                filled_any = True
                self.say(f"{tag} filled name")

            # Fill age
            if age_text:
                if await self._fill_profile_field(
                    value=age_text,
                    label_keywords=["年龄", "Age"],
                    selectors=[
                        'input[name*="age" i]',
                        'input[id*="age" i]',
                        'input[placeholder*="age" i]',
                        'input[aria-label*="age" i]',
                        'input[placeholder*="年龄"]',
                        'input[aria-label*="年龄"]',
                        'input[name*="年龄"]',
                    ],
                ):
                    filled_any = True
                    self.say(f"{tag} filled age")

            # Single birthday field
            if y and m and d:
                if await self._fill_first_visible_input(
                    [
                        'input[type="date"]',
                        'input[name*="birth" i]',
                        'input[id*="birth" i]',
                        'input[placeholder*="birthday" i]',
                        'input[placeholder*="出生" i]',
                    ],
                    f"{y}-{m}-{d}",
                ):
                    filled_any = True

            # Split birthday fields
            if y:
                filled_any = (await self._fill_first_visible_input(['input[name*="year" i]', 'input[placeholder*="year" i]'], y)) or filled_any
            if m:
                filled_any = (await self._fill_first_visible_input(['input[name*="month" i]', 'input[placeholder*="month" i]'], m)) or filled_any
            if d:
                filled_any = (await self._fill_first_visible_input(['input[name*="day" i]', 'input[placeholder*="day" i]'], d)) or filled_any

            if not filled_any:
                body_hints = (
                    "about you",
                    "tell us",
                    "your name",
                    "birthday",
                    "birth",
                    "age",
                    "关于你",
                    "你的姓名",
                    "出生",
                    "年龄",
                )
                still_on_profile = any(k in body for k in body_hints)
                on_auth = "auth.openai.com" in (self.page.url or "").lower()
                combined = await self._is_combined_verification_profile_page()
                if (still_on_profile or combined) and on_auth:
                    self.say(f"{tag} fields already filled on step {step}, retry submit")
                    before = self.page.url
                    await self.sleep(120)
                    try:
                        await self.click_button_by_text(
                            ["Continue", "继续", "下一步", "Submit", "提交"],
                            timeout_ms=1_500,
                        )
                    except Exception:
                        await self.click_submit_button()
                    await self.wait_for_url_change(before, timeout_ms=3_000)
                    try:
                        await self.page.wait_for_load_state("domcontentloaded", timeout=1_200)
                    except Exception:
                        pass
                    await self.wait_for_cloudflare(2_500)
                    continue
                self.say(f"{tag} no fillable fields on step {step}, skip submit")
                break

            before = self.page.url
            await self.sleep(120)
            try:
                await self.click_button_by_text(["Continue", "继续", "下一步", "Submit", "提交"], timeout_ms=1_500)
            except Exception:
                await self.click_submit_button()
            await self.wait_for_url_change(before, timeout_ms=1_500)
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=1_200)
            except Exception:
                pass
            await self.wait_for_cloudflare(2_500)
            if await self._has_profile_validation_errors():
                self.say(f"{tag} detected profile validation error, retry refill")
                await self._fill_profile_field(
                    value=full_name,
                    label_keywords=["全名", "姓名", "Name", "Full name"],
                    selectors=[
                        'input[name*="name" i]',
                        'input[placeholder*="name" i]',
                        'input[id*="name" i]',
                        'input[placeholder*="姓名"]',
                        'input[aria-label*="姓名"]',
                    ],
                )
                if age_text:
                    await self._fill_profile_field(
                        value=age_text,
                        label_keywords=["年龄", "Age"],
                        selectors=[
                            'input[name*="age" i]',
                            'input[id*="age" i]',
                            'input[placeholder*="age" i]',
                            'input[aria-label*="age" i]',
                            'input[placeholder*="年龄"]',
                            'input[aria-label*="年龄"]',
                            'input[name*="年龄"]',
                        ],
                    )
                try:
                    await self.click_button_by_text(["Continue", "继续", "下一步", "Submit", "提交"], timeout_ms=1_500)
                except Exception:
                    await self.click_submit_button()

    async def select_country(self, dial_code: str, country_name: str = "", iso_code: str = "") -> None:
        # best-effort: country picker is dynamic; do not hard-fail if not found
        targets = [dial_code.strip(), country_name.strip(), iso_code.strip()]
        targets = [t for t in targets if t]
        if not targets:
            return
        try:
            await self.click_button_by_text(["country", "国家", "地区", "region"], 5000)
        except Exception:
            pass
        for t in targets:
            try:
                await self.click_button_by_text([t], 3000)
                return
            except Exception:
                continue

    def get_local_phone_number(self, phone_number: str, country: Any) -> str:
        number = re.sub(r"\D+", "", str(phone_number or ""))
        dial = re.sub(r"\D+", "", str(getattr(country, "dial_code", "") or ""))
        if dial and number.startswith(dial):
            return number[len(dial):] or number
        return number

    async def enter_phone(self, local_number: str) -> None:
        inp = self.page.locator('input[name="phoneNumberInput"], input[type="tel"]').first
        await inp.wait_for(timeout=20_000)
        await inp.click(click_count=3)
        await inp.fill("")
        await inp.type(local_number, delay=35)
        await self.sleep(500)
        await self.click_submit_button()

    async def complete_profile(
        self,
        profile: Any,
        sms_code_callback: SMS_CODE_CALLBACK,
        *,
        skip_about_you: bool = False,
    ) -> bool:
        code = await sms_code_callback()
        if not code:
            raise RuntimeError("empty sms code")
        await self.enter_sms_code(code)
        await self.click_submit_button()
        await self.wait_for_cloudflare(30_000)
        if skip_about_you:
            return True

        await self.fill_password_if_shown(getattr(profile, "password", ""))
        await self.sleep(1200)
        await self.fill_about_you_and_submit(
            getattr(profile, "full_name", ""),
            getattr(profile, "age", ""),
            getattr(profile, "birth_date", ""),
            "[Phase1]",
            verification_code=code,
        )
        return True

    async def navigate_to_oauth(self, auth_url: str) -> None:
        await self.page.goto(auth_url, wait_until="domcontentloaded", timeout=90_000)
        await self.wait_for_cloudflare(45_000)

    def _is_redirect_callback(self, url: str, redirect_base: Any) -> bool:
        current = urlparse(url)
        query = current.query or ""
        return (
            current.hostname == redirect_base.hostname
            and str(current.port or "") == str(redirect_base.port or "")
            and current.path == redirect_base.path
            and ("code=" in query or "error=" in query)
        )

    async def _fill_login_form_if_present(self, email: str, password: str) -> bool:
        email_loc = self.page.locator('input[type="email"], input[name="email"], input[name="username"]')
        pass_loc = self.page.locator('input[type="password"]')

        if await email_loc.count() > 0:
            box = email_loc.first
            await box.click(click_count=3)
            await box.fill("")
            await box.type(email, delay=35)
            await self.click_submit_button()
            await self.sleep(1000)
            return True

        if await pass_loc.count() > 0 and password:
            box = pass_loc.first
            await box.click(click_count=3)
            await box.fill("")
            await box.type(password, delay=35)
            await self.click_submit_button()
            await self.sleep(1000)
            return True

        return False

    async def oauth_login_and_authorize(self, options: dict[str, Any]) -> str:
        redirect_uri = str(options.get("redirectUri") or "").strip()
        if not redirect_uri:
            raise RuntimeError("oauth_login_and_authorize: redirectUri is required")

        redirect_base = urlparse(redirect_uri)
        email = str(options.get("email") or "")
        password = str(options.get("password") or "")
        on_sms_needed = options.get("onSmsNeeded")
        on_email_code_needed = options.get("onEmailCodeNeeded")

        deadline = time.monotonic() + 420
        while time.monotonic() < deadline:
            if await self.recover_auth_route_error(max_attempts=2, wait_ms=10_000):
                continue
            url = self.page.url
            if self._is_redirect_callback(url, redirect_base):
                return url

            low_text = ""
            try:
                low_text = (await self.page.evaluate("() => (document.body?.innerText || '').toLowerCase()"))
            except Exception:
                pass

            if await self._fill_login_form_if_present(email, password):
                await self.wait_for_cloudflare(20_000)
                continue

            if on_email_code_needed and (
                "email verification" in low_text
                or "verification code" in low_text
                or "邮箱验证码" in low_text
            ):
                code = await on_email_code_needed()
                if code:
                    await self.enter_sms_code(code)
                    await self.click_submit_button()
                    await self.sleep(1200)
                    continue

            if on_sms_needed and ("sms" in low_text or "phone verification" in low_text or "手机号" in low_text):
                code = await on_sms_needed()
                if code:
                    await self.enter_sms_code(code)
                    await self.click_submit_button()
                    await self.sleep(1200)
                    continue

            # OAuth consent page
            try:
                await self.click_button_by_text(["Continue", "Allow", "Authorize", "同意", "继续", "授权"], 2000)
                await self.wait_for_cloudflare(20_000)
                await self.sleep(1200)
                continue
            except Exception:
                pass

            await self.sleep(1000)

        raise RuntimeError("OAuth login/authorize timeout")
