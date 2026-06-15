from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from .grizzly_sms_provider import GrizzlySMSProvider
from .hero_sms_provider import HeroSMSProvider, PhoneCountry, SmsActivation, local_phone_number
from .mail_provider import MailProvider
from .storage import MailAccount
from .utils import log, random_profile


class FatalAccountError(RuntimeError):
    pass


class ManualInterventionNeeded(RuntimeError):
    pass


AUTH_RETRY_ERROR_HINTS = (
    "route error",
    "invalid content type",
    "糟糕，出错了",
    "oops, an error occurred",
    "something went wrong",
)

AUTH_RETRY_BUTTON_TEXTS = ("重试", "再试一次", "Retry", "Try again")


class ChatGPTRegister:
    def __init__(
        self,
        page: Page,
        page_getter: Callable[[], Awaitable[Page]] | None,
        start_url: str,
        entry_action: str,
        mail_provider: MailProvider,
        age_min: int,
        age_max: int,
        sms_selection: dict[str, object] | None = None,
        log_prefix: str = "",
    ):
        self.page = page
        self.page_getter = page_getter
        self.start_url = start_url
        self.entry_action = entry_action
        self.mail_provider = mail_provider
        self.age_min = age_min
        self.age_max = age_max
        self.sms_selection = sms_selection
        self.log_prefix = log_prefix
        self.generated_name: str | None = None
        self.generated_age: str | None = None
        self.bad_codes: set[str] = set()
        self.unknown_count = 0
        self.entry_count = 0
        self.auth_error_retry_count = 0

    def log(self, message: str) -> None:
        log(f"{self.log_prefix} {message}".strip())

    async def run_until_logged_in(self, account: MailAccount, since: datetime) -> None:
        if self.start_url != "current":
            await self.page.goto(self.start_url, wait_until="domcontentloaded")
        for step in range(1, 50):
            try:
                await self.refresh_page()
                await self.page.wait_for_load_state("domcontentloaded")
                state = await self.detect_state()
                self.log(f"注册状态[{step}]: {state} | url={short_url(self.page.url)}")
                if state == "fatal_account_error":
                    if await self.retry_auth_error_page(max_attempts=2):
                        continue
                    raise FatalAccountError(await fatal_error_message(self.page))
                self.auth_error_retry_count = 0
                if state == "logged_in":
                    self.log("已确认登录成功")
                    return
                if state == "external_oauth":
                    self.log("检测到误入第三方登录页，返回 ChatGPT 登录入口")
                    await self.page.goto(self.start_url, wait_until="domcontentloaded")
                    continue
                if state == "entry":
                    self.entry_count += 1
                    if self.entry_count >= 3 and self.entry_action.lower() in {"signup_phone", "phone_signup", "phone"}:
                        self.log("入口页: 多次点击未推进，直接打开登录页再切手机注册")
                        await self.page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded")
                        await settle(self.page)
                        continue
                    self.log(f"入口页: 点击 {self.entry_action}")
                    await self.click_entry()
                    continue
                if state == "phone_login":
                    if not self.sms_selection:
                        raise FatalAccountError("账号进入手机号登录/注册页，未启用手机号接码，按规则废弃当前账号")
                    self.log("手机号页: 已启用接码，开始自动获取手机号")
                    await self.handle_phone_required()
                    continue
                if state == "email":
                    if self.entry_action.lower() in {"signup_phone", "phone_signup", "phone"} and self.sms_selection:
                        self.log("邮箱页: Free 注册配置为手机号优先，尝试切换到手机登录")
                        if await self.click_phone_switch():
                            continue
                    self.log(f"邮箱页: 填入邮箱 {account.email}")
                    await self.fill_email(account.email)
                    continue
                if state == "password":
                    self.log("密码页: 填入账号密码")
                    await self.fill_password(account)
                    continue
                if state == "code":
                    self.log("验证码页: 开始拉取邮箱验证码")
                    code = await self.mail_provider.wait_code(account, since, self.bad_codes)
                    self.log(f"验证码页: 已拿到验证码 {code}，准备填入")
                    accepted = await self.fill_code(code)
                    if not accepted:
                        self.bad_codes.add(code)
                        self.log(f"验证码被页面判定无效，已排除旧码: {code}")
                    else:
                        self.log("验证码页: 已提交验证码")
                    continue
                if state == "profile":
                    self.log("资料页: 准备填写姓名和年龄")
                    await self.fill_profile()
                    continue
                if state == "captcha_or_unknown":
                    self.unknown_count += 1
                    await dump_unknown_page(self.page, self.unknown_count)
                    if self.unknown_count >= 4:
                        raise ManualInterventionNeeded("连续检测到未知页/人工验证，账号已退回号池")
                    self.log(f"检测到未知页/人工验证，等待页面自动推进后重试 | 次数={self.unknown_count}/4")
                    await self.page.wait_for_timeout(3000)
                    continue
                self.unknown_count = 0
            except FatalAccountError:
                raise
            except Exception as exc:
                if is_page_closed_error(exc):
                    self.log("页面切换/关闭中，重新绑定当前页面")
                    await self.refresh_page()
                    await self.page.wait_for_timeout(1000)
                    continue
                raise
        raise RuntimeError("注册状态机循环次数过多，疑似卡住")

    async def refresh_page(self) -> None:
        if self.page_getter:
            self.page = await self.page_getter()

    async def retry_auth_error_page(self, *, max_attempts: int = 2) -> bool:
        text = await body_text(self.page)
        reason = recoverable_auth_error_reason(text.lower(), text)
        if not reason or self.auth_error_retry_count >= max_attempts:
            return False
        self.auth_error_retry_count += 1
        self.log(f"认证错误页({reason})，点击“重试”恢复 ({self.auth_error_retry_count}/{max_attempts})")
        clicked = await click_auth_retry_button(self.page)
        if not clicked:
            self.log("认证错误页未找到“重试”按钮，尝试刷新当前页")
            try:
                await self.page.reload(wait_until="domcontentloaded", timeout=20_000)
            except Exception as exc:
                self.log(f"刷新认证错误页失败: {short_error_text(exc)}")
        deadline = asyncio.get_running_loop().time() + 12
        while asyncio.get_running_loop().time() < deadline:
            await self.page.wait_for_timeout(700)
            text = await body_text(self.page)
            if not recoverable_auth_error_reason(text.lower(), text):
                self.log("认证错误页重试后已恢复")
                return True
        return True

    async def detect_state(self) -> str:
        url = self.page.url.lower()
        text = await body_text(self.page)
        low = text.lower()
        if any(host in url for host in ["accounts.google.", "appleid.apple.", "login.microsoftonline."]):
            return "external_oauth"
        if is_fatal_account_error(low, text):
            return "fatal_account_error"
        if "404" in text and "找不到页面" in text:
            return "entry"
        if "chatgpt.com" in url and (
            "/g/" in url
            or "/c/" in url
            or await chatgpt_logged_in_markers(self.page, low, text)
        ):
            return "logged_in"
        if await is_phone_login_page(self.page, low, text):
            return "phone_login"
        if is_entry_page(low, text):
            return "entry"
        if await visible_input_count(self.page, r"email|username") > 0:
            return "email"
        if await visible_input_count(self.page, r"password") > 0 and not likely_code_page(low):
            return "password"
        if likely_code_page(low) or await visible_code_inputs(self.page) > 0:
            return "code"
        if any(key in low for key in ["tell us about yourself", "full name", "birthday", "date of birth", "age"]) or any(
            key in text for key in ["姓名", "名字", "年龄", "生日", "出生"]
        ):
            return "profile"
        return "captcha_or_unknown"

    async def click_entry(self) -> None:
        entry_action = self.entry_action.lower()
        if entry_action in {"login", "signin", "log_in"}:
            labels = ["登录", "Log in", "Login", "Sign in"]
            patterns = [re.compile(r"log in|login|sign in", re.I), re.compile(r"登录")]
        elif entry_action in {"signup_phone", "phone_signup", "phone"}:
            labels = ["登录", "Log in", "Login", "Sign in", "免费注册", "创建账号", "注册", "Sign up", "Create account"]
            patterns = [re.compile(r"log in|login|sign in|sign up|create account|register", re.I), re.compile(r"登录|免费注册|创建账号|注册")]
        else:
            labels = ["免费注册", "创建账号", "创建帐户", "注册", "Sign up for free", "Sign up", "Create account", "Register"]
            patterns = [re.compile(r"sign up|create account|create|register|free", re.I), re.compile(r"免费注册|创建账号|创建帐户|注册|创建")]
        for pattern in patterns:
            buttons = self.page.get_by_role("button", name=pattern)
            for index in range(await buttons.count()):
                button = buttons.nth(index)
                try:
                    if await button.is_visible() and await button.is_enabled():
                        await button.click()
                        await settle(self.page)
                        return
                except Exception:
                    continue
            links = self.page.get_by_role("link", name=pattern)
            for index in range(await links.count()):
                link = links.nth(index)
                try:
                    if await link.is_visible():
                        await link.click()
                        await settle(self.page)
                        return
                except Exception:
                    continue
        for label in labels:
            if await click_by_visible_text(self.page, label):
                await settle(self.page)
                return
        clicked = await self.page.evaluate(
            """(labels) => {
                const normalized = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const wanted = labels.map(normalized);
                const nodes = [...document.querySelectorAll('button, a, [role="button"], div, span')];
                for (const node of nodes) {
                    const text = normalized(node.innerText || node.textContent || '');
                    if (!text || !wanted.some((label) => text === label || text.includes(label))) continue;
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    if (rect.width <= 0 || rect.height <= 0 || style.visibility === 'hidden' || style.display === 'none') continue;
                    node.click();
                    return true;
                }
                return false;
            }""",
            labels,
        )
        if clicked:
            await settle(self.page)
            return
        if entry_action in {"signup_phone", "phone_signup", "phone"}:
            clicked = await self.page.evaluate(
                """() => {
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const nodes = [...document.querySelectorAll('a[href], button, [role="button"]')].filter(visible);
                    const target = nodes.find((el) => /login|log in|登录|sign up|注册|创建/i.test(el.innerText || el.textContent || el.getAttribute('aria-label') || ''))
                        || nodes.find((el) => String(el.getAttribute('href') || '').includes('/auth/login'));
                    if (!target) return false;
                    target.click();
                    return true;
                }"""
            )
            if clicked:
                await settle(self.page)
                return
        raise RuntimeError(f"入口页未找到可点击按钮: entry_action={self.entry_action}")

    async def click_email_switch(self) -> None:
        if await click_by_visible_text(self.page, "继续使用电子邮件地址登录"):
            await settle(self.page)
            return
        if await click_by_visible_text(self.page, "Continue with email"):
            await settle(self.page)
            return
        clicked = await self.page.evaluate(
            """() => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const nodes = [...document.querySelectorAll('button, a, [role="button"], div')];
                const target = nodes.find((el) => visible(el) && /电子邮件|email/i.test(el.innerText || el.textContent || ''));
                if (!target) return false;
                target.click();
                return true;
            }"""
        )
        if clicked:
            await settle(self.page)
            return
        raise RuntimeError("电话登录页未找到切换到邮箱登录按钮")

    async def click_phone_switch(self) -> bool:
        labels = ["使用电话号码继续", "使用手机号继续", "手机登录", "手机号登录", "继续使用手机登录", "Continue with phone", "Continue with phone number", "Phone number", "Phone"]
        for label in labels:
            if await click_by_visible_text(self.page, label):
                await settle(self.page)
                return True
        clicked = await self.page.evaluate(
            """() => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const nodes = [...document.querySelectorAll('button, a, [role="button"], div')];
                const target = nodes.find((el) => visible(el) && /手机|手机号|电话号码|phone/i.test(el.innerText || el.textContent || ''));
                if (!target) return false;
                target.click();
                return true;
            }"""
        )
        if clicked:
            await settle(self.page)
            return True
        return False

    async def fill_email(self, email: str) -> None:
        locator = await first_visible(
            self.page.locator("input[type='email'], input[name*='email' i], input[autocomplete='username']")
        )
        if not locator:
            locator = await first_textbox(self.page)
        if not locator:
            raise RuntimeError("未找到邮箱输入框")
        await human_fill(locator, email, force_mouse=True)
        if not await click_email_submit(self.page, locator):
            raise RuntimeError("邮箱页未找到安全的继续按钮")

    async def fill_password(self, account: MailAccount) -> None:
        if not account.password:
            raise FatalAccountError("账号进入密码页，但账号池没有 password；判定为已注册/不可用，跳过当前账号")
        locator = await first_visible(self.page.locator("input[type='password']"))
        if not locator:
            raise RuntimeError("未找到密码输入框")
        await human_fill(locator, account.password, force_mouse=True)
        await click_submit_by_js(self.page, ["下一步", "继续", "登录", "Continue", "Next", "Log in"])

    async def fill_code(self, code: str) -> bool:
        inputs = await visible_locators(self.page.locator("input:not([type='file'])"))
        code_inputs = []
        for item in inputs:
            attrs = await input_attrs(item)
            if "password" in attrs.get("type", "").lower():
                continue
            if any(k in " ".join(attrs.values()).lower() for k in ["code", "otp", "verification", "one-time"]):
                code_inputs.append(item)
        if len(code_inputs) >= 6:
            for index, char in enumerate(code[:6]):
                await code_inputs[index].fill(char)
        elif code_inputs:
            await human_fill(code_inputs[0], code, force_mouse=True)
        else:
            target = await first_textbox(self.page)
            if not target:
                raise RuntimeError("未找到验证码输入框")
            await human_fill(target, code, force_mouse=True)
        await click_continue(self.page)
        return not await is_invalid_code_page(self.page)

    async def fill_profile(self) -> None:
        if not self.generated_name or not self.generated_age:
            self.generated_name, self.generated_age = random_profile(self.age_min, self.age_max)
            self.log(f"已生成资料: {self.generated_name} / {self.generated_age}")
        if await fill_profile_by_js(self.page, self.generated_name, self.generated_age, self.log):
            await click_profile_submit_by_js(self.page)
            self.log("资料页: 已点击完成创建")
            return
        raise RuntimeError("资料页未能自动填入姓名和年龄")

    async def handle_phone_required(self) -> None:
        selection = self.sms_selection or {}
        provider_name = str(selection.get("provider") or "herosms").lower()
        if provider_name in {"fivesim", "5sim"}:
            provider_name = "fivesim"
        default_label = {"grizzly": "GrizzlySMS", "fivesim": "5sim"}.get(provider_name, "HeroSMS")
        provider_label = str(selection.get("provider_label") or default_label)
        api_key = str(selection.get("api_key") or "").strip()
        default_service = "openai" if provider_name == "fivesim" else "dr"
        service = str(selection.get("service") or default_service).strip() or default_service
        country = selection.get("country")
        operator = selection.get("operator")
        if not api_key or not isinstance(country, PhoneCountry):
            raise FatalAccountError("手机号接码配置不完整，无法处理手机号必填页")
        operator_value = str(getattr(operator, "operator", "") or "").strip()
        # 5sim 不接受空 operator，默认 "any"
        if provider_name == "fivesim" and not operator_value:
            operator_value = "any"
        operator_label = str(getattr(operator, "label", "") or operator_value or "任何运营商")
        poll_interval = float(selection.get("poll_interval") or 5.0)
        max_attempts = int(selection.get("max_attempts") or 60)
        if provider_name == "grizzly":
            provider = GrizzlySMSProvider(api_key)
        elif provider_name == "fivesim":
            from .fivesim_sms_provider import FiveSimProvider

            provider = FiveSimProvider(api_key)
        else:
            provider = HeroSMSProvider(api_key)
        # 5sim 用 slug；HeroSMS/Grizzly 用 hero_sms_country int
        country_arg = country if provider_name == "fivesim" else country.hero_sms_country
        activation: SmsActivation | None = None
        try:
            self.log(
                f"手机号页: {provider_label} 自动接码启动 | service={service}, "
                f"国家={country.name}(+{country.dial_code}, ID={country.hero_sms_country}), 服务商={operator_label}"
            )
            activation = await asyncio.to_thread(
                provider.get_number,
                service,
                country_arg,
                operator=operator_value,
            )
            self.log(f"手机号页: 已获取手机号 {activation.phone_number}，activation={activation.activation_id}")
            await asyncio.to_thread(provider.mark_ready, activation.activation_id)
            await fill_phone_and_wait_sms_page(self.page, activation.phone_number, country, self.log)
            if await page_looks_like_create_password(self.page):
                password = str(selection.get("password") or "").strip()
                if not password:
                    raise RuntimeError("手机号注册进入创建密码页，但当前流程未提供密码")
                self.log("手机号页: 检测到创建密码页，先填入注册密码")
                await fill_create_password_page(self.page, password, self.log)
            if await wait_for_sms_verification_page(self.page, self.log) != "sms":
                self.log("手机号页: 未明确识别到短信验证码页，仍继续尝试拉取验证码")
            self.log(f"手机号页: 开始拉取短信验证码，间隔={poll_interval:g}s，最多={max_attempts}次")
            code = await asyncio.to_thread(
                provider.poll_for_code,
                activation.activation_id,
                interval=poll_interval,
                max_attempts=max_attempts,
            )
            self.log(f"手机号页: 已拉取到短信验证码 {code}，准备填入")
            await fill_sms_code(self.page, code, self.log)
            status, detail = await wait_for_code_submit_result(self.page, timeout=12)
            if status == "invalid":
                raise RuntimeError(f"短信验证码无效或过期: {detail}")
            if status == "pending":
                self.log("手机号页: 验证码已提交，页面暂未明确推进，继续状态机观察")
            else:
                self.log("手机号页: 验证码提交成功，页面已推进")
            if selection.get("defer_sms_complete"):
                selection["last_phone"] = activation.phone_number
                selection["last_activation"] = activation
                selection["last_sms_code"] = code
                self.log("手机号页: 当前流程要求延后完成短信激活")
            else:
                await asyncio.to_thread(provider.complete, activation.activation_id)
        except Exception as exc:
            if activation:
                await asyncio.to_thread(provider.cancel, activation.activation_id)
                selection.pop("last_activation", None)
            raise FatalAccountError(f"手机号接码失败: {exc}") from exc


async def find_phone_input(page: Page) -> Locator | None:
    selectors = (
        'input[name="local_number"]',
        'input[name="phoneNumberInput"]',
        'input[type="tel"]',
        'input[autocomplete="tel"]',
        'input[name*="phone" i]',
        'input[placeholder*="phone" i]',
        'input[placeholder*="手机号"]',
        'input[placeholder*="电话号码"]',
    )
    for selector in selectors:
        found = await maybe_visible_selector(page, selector, timeout=900)
        if found:
            return found
    return None


async def select_phone_country(page: Page, country: PhoneCountry, logger: Callable[[str], None]) -> None:
    if not country.dial_code and not country.iso_code:
        return
    if await page_has_split_phone_form(page):
        logger("手机号页: 分栏表单无需下拉选择国家")
        return
    logger(f"手机号页: 选择国家 {country.name} +{country.dial_code}")
    try:
        already = await page.evaluate(
            """(code) => {
                for (const node of document.querySelectorAll('button, select')) {
                    const text = (node.innerText || node.textContent || '').trim();
                    if (text.includes(`+${code}`) || text.includes(`(${code})`)) return text;
                }
                return '';
            }""",
            country.dial_code,
        )
        if already:
            logger(f"手机号页: 页面国家已匹配 {already}")
            return
    except Exception:
        pass

    try:
        changed = await page.evaluate(
            """({ iso, code, name }) => {
                const select = document.querySelector('select');
                if (!select) return '';
                const options = Array.from(select.options || []);
                let target = null;
                if (iso) target = options.find(opt => String(opt.value || '').toUpperCase() === iso);
                if (!target && name) target = options.find(opt => (opt.text || '').includes(name));
                if (!target && code) target = options.find(opt => (opt.text || '').includes(`+${code}`) || (opt.text || '').includes(`+(${code})`) || (opt.text || '').includes(`(${code})`));
                if (!target) return '';
                const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
                if (setter) setter.call(select, target.value);
                else select.value = target.value;
                select.dispatchEvent(new Event('input', { bubbles: true }));
                select.dispatchEvent(new Event('change', { bubbles: true }));
                for (const b of document.querySelectorAll('button')) {
                    const text = (b.innerText || b.textContent || '').trim();
                    if (text.includes(`+${code}`) || text.includes(`+(${code})`) || text.includes(`(${code})`)) return text;
                }
                return target.text || target.value;
            }""",
            {"iso": country.iso_code, "code": country.dial_code, "name": country.name},
        )
        if changed and (
            f"+{country.dial_code}" in changed
            or f"+({country.dial_code})" in changed
            or f"({country.dial_code})" in changed
            or country.iso_code in changed
            or country.name in changed
        ):
            logger(f"手机号页: 已通过 select 选择国家 {changed}")
            await settle(page)
            if await country_selector_matches(page, country):
                return
    except Exception as exc:
        logger(f"手机号页: select 国家选择失败，继续备用方式: {short_error_text(exc)}")

    try:
        button = page.locator('button[aria-haspopup="listbox"]').filter(has_text=re.compile(r"\+\(?\d")).first
        if await button.count() > 0 and await button.is_visible():
            await button.click(timeout=3000)
            await page.wait_for_timeout(1500)
            target = await page.evaluate(
                """({ iso, code, name }) => {
                    const select = document.querySelector('select');
                    const options = Array.from(select?.options || []);
                    let index = -1;
                    let value = iso || '';
                    for (let i = 0; i < options.length; i += 1) {
                        const text = options[i].text || '';
                        const optionValue = String(options[i].value || '').toUpperCase();
                        if ((iso && optionValue === iso) || (name && text.includes(name)) || (code && (text.includes(`+${code}`) || text.includes(`+(${code})`) || text.includes(`(${code})`)))) {
                            index = i;
                            value = options[i].value || value;
                            break;
                        }
                    }
                    return { index, value };
                }""",
                {"iso": country.iso_code, "code": country.dial_code, "name": country.name},
            )
            if isinstance(target, dict) and int(target.get("index", -1)) >= 0:
                await page.evaluate(
                    """(idx) => {
                        const listbox = document.querySelector('[role="listbox"]');
                        if (!listbox) return;
                        let scroller = listbox;
                        while (scroller && scroller !== document.body) {
                            const style = getComputedStyle(scroller);
                            if (style.overflow === 'auto' || style.overflow === 'scroll' || style.overflowY === 'auto' || style.overflowY === 'scroll') break;
                            scroller = scroller.parentElement;
                        }
                        if (scroller) scroller.scrollTop = idx * 40;
                    }""",
                    int(target["index"]),
                )
                await page.wait_for_timeout(1000)
                value = str(target.get("value") or country.iso_code)
                option_box = await page.evaluate(
                    """({ value, code, name }) => {
                        const visible = (el) => {
                            const rect = el.getBoundingClientRect();
                            const style = getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                        };
                        const selectors = value ? [`[data-key="${CSS.escape(value)}"]`, `[data-value="${CSS.escape(value)}"]`, `[value="${CSS.escape(value)}"]`] : [];
                        for (const selector of selectors) {
                            const option = document.querySelector(selector);
                            if (option && visible(option)) {
                                const rect = option.getBoundingClientRect();
                                return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, text: option.innerText || option.textContent || value };
                            }
                        }
                        for (const option of document.querySelectorAll('[role="option"], li, div')) {
                            const text = (option.innerText || option.textContent || '').trim();
                            if (!visible(option)) continue;
                            if ((name && text.includes(name)) || (code && (text.includes(`+${code}`) || text.includes(`+(${code})`) || text.includes(`(${code})`)))) {
                                const rect = option.getBoundingClientRect();
                                return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, text };
                            }
                        }
                        return null;
                    }""",
                    {"value": value, "code": country.dial_code, "name": country.name},
                )
                if option_box:
                    await page.mouse.click(option_box["x"], option_box["y"])
                    logger(f"手机号页: 已通过下拉选择国家 {option_box.get('text')}")
                    await settle(page)
                    if await country_selector_matches(page, country):
                        return
    except Exception as exc:
        logger(f"手机号页: 下拉国家选择失败: {short_error_text(exc)}")
    raise RuntimeError(f"国家选择失败，未能切换到 {country.name} +{country.dial_code}")


async def country_selector_matches(page: Page, country: PhoneCountry) -> bool:
    try:
        current = await current_phone_country_code(page)
        return bool(country.dial_code and current == country.dial_code)
    except Exception:
        return False


async def click_phone_submit(page: Page, field: Locator | None = None) -> bool:
    clicked = await click_submit_by_js(
        page,
        [
            "发送验证码",
            "Send code",
            "Send verification code",
            "继续",
            "Continue",
            "Next",
            "Verify",
            "Submit",
            "验证",
            "下一步",
        ],
    )
    if clicked:
        return True
    if field is not None:
        try:
            await field.press("Enter")
            await settle(page)
            return True
        except Exception:
            pass
    return False


async def page_has_split_phone_form(page: Page) -> bool:
    url = (page.url or "").lower()
    if "radar-challenge" in url:
        return True
    try:
        return bool(
            await page.evaluate(
                """() => {
                    const cc = document.querySelector('input[name="country_code"]');
                    const ln = document.querySelector('input[name="local_number"]');
                    if (!cc || !ln) return false;
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0
                            && style.visibility !== 'hidden'
                            && style.display !== 'none';
                    };
                    return visible(cc) && visible(ln);
                }"""
            )
        )
    except Exception:
        return False


async def wait_split_phone_form(page: Page, timeout_ms: int = 20_000) -> None:
    await page.wait_for_selector('input[name="country_code"]', state="visible", timeout=timeout_ms)
    await page.wait_for_selector('input[name="local_number"]', state="visible", timeout=timeout_ms)


async def fill_react_input(locator: Locator, value: str) -> None:
    await locator.scroll_into_view_if_needed()
    await locator.click(force=True)
    await locator.fill("")
    await locator.fill(str(value))
    await locator.evaluate(
        """(el, val) => {
            const proto = HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (setter) setter.call(el, val);
            else el.value = val;
            el.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('blur', { bubbles: true }));
        }""",
        str(value),
    )


async def read_input_value(locator: Locator) -> str:
    try:
        return (await locator.input_value()).strip()
    except Exception:
        return ""


async def read_input_value(locator: Locator) -> str:
    try:
        return (await locator.input_value()).strip()
    except Exception:
        return ""


def _phone_digits(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


async def _clear_and_type(locator: Locator, text: str, *, delay_ms: int = 80) -> None:
    await locator.scroll_into_view_if_needed()
    await locator.click(force=True)
    await locator.page.keyboard.press("Control+A")
    await locator.page.keyboard.press("Backspace")
    await locator.press_sequentially(str(text), delay=delay_ms)


async def _run_before_auto_send(
    before_auto_send: Callable[[], Awaitable[None] | None] | None,
) -> None:
    if before_auto_send is None:
        return
    result = before_auto_send()
    if asyncio.iscoroutine(result):
        await result


async def fill_split_phone_form(
    page: Page,
    phone: str,
    country: PhoneCountry,
    logger: Callable[[str], None],
    *,
    before_auto_send: Callable[[], Awaitable[None] | None] | None = None,
) -> None:
    await wait_split_phone_form(page)
    country_input = page.locator('input[name="country_code"]').first
    local_input = page.locator('input[name="local_number"]').first
    dial = re.sub(r"\D+", "", country.dial_code or "")
    dial_display = f"+{dial}" if dial else "+"
    local_value = local_phone_number(phone, country)
    logger(
        f"手机号页: Cursor 分栏表单 | 区号={dial_display} | 本地号码={local_value} | "
        f"国家={country.name}(+{country.dial_code or '-'})"
    )

    await _clear_and_type(country_input, dial_display, delay_ms=60)
    await page.wait_for_timeout(400)
    cc_val = await read_input_value(country_input)
    if _phone_digits(cc_val) != dial:
        logger(f"手机号页: 区号未生效(当前={cc_val!r})，重试填入")
        await _clear_and_type(country_input, dial_display, delay_ms=60)
        cc_val = await read_input_value(country_input)

    await country_input.evaluate("el => el.blur()")
    await page.wait_for_timeout(300)
    await _clear_and_type(local_input, local_value, delay_ms=90)
    await page.wait_for_timeout(500)
    local_val = await read_input_value(local_input)

    if _phone_digits(local_val) != local_value:
        logger(f"手机号页: 本地号码未生效(当前={local_val!r})，逐位重试")
        await local_input.click(force=True)
        await page.wait_for_timeout(200)
        await local_input.fill("")
        await local_input.press_sequentially(local_value, delay=100)
        await page.wait_for_timeout(500)
        local_val = await read_input_value(local_input)

    cc_val = await read_input_value(country_input)
    if _phone_digits(cc_val) != dial:
        raise RuntimeError(f"区号填入失败: 期望={dial_display}, 实际={cc_val or '(空)'}")
    if _phone_digits(local_val) != local_value:
        raise RuntimeError(
            f"本地号码填入失败: 期望={local_value}, 实际={local_val or '(空)'} "
            f"(digits={_phone_digits(local_val)})"
        )
    logger(f"手机号页: 本地号码已填入 ({local_val})，准备点击提交")
    await _run_before_auto_send(before_auto_send)
    if not await click_phone_submit(page, local_input):
        raise RuntimeError("手机号页: 未找到提交按钮")
    logger("手机号页: 已点击提交，等待跳转并发送短信验证码")
    await page.wait_for_timeout(2000)


async def fill_phone_and_wait_sms_page(
    page: Page,
    phone: str,
    country: PhoneCountry,
    logger: Callable[[str], None],
    *,
    before_auto_send: Callable[[], Awaitable[None] | None] | None = None,
) -> None:
    logger(f"手机号页: 准备填入手机号 | 国家={country.name}, ISO={country.iso_code or '-'}, 区号=+{country.dial_code or '-'}")
    if await page_has_split_phone_form(page):
        logger("手机号页: 检测到 Cursor 分栏手机表单 (country_code + local_number)")
        await fill_split_phone_form(page, phone, country, logger, before_auto_send=before_auto_send)
        return
    phone_input = await find_phone_input(page)
    if not phone_input:
        url = (page.url or "").lower()
        if "magic-code" in url:
            raise RuntimeError("当前为 magic-code 验证码页，不是手机号填写页")
        raise RuntimeError("未找到手机号输入框")
    await select_phone_country(page, country, logger)
    logger("手机号页: 已找到手机号输入框")
    current_country = await current_phone_country_code(page)
    if country.dial_code and current_country != country.dial_code:
        raise RuntimeError(f"国家选择未生效：页面=+{current_country or '-'}，目标=+{country.dial_code}")
    value = local_phone_number(phone, country)
    logger(f"手机号页: 接码号码={phone}，页面国家=+{current_country or '-'}，输入本地号码={value}")
    await human_fill(phone_input, value, force_mouse=True)
    logger("手机号页: 手机号已填入，准备点击提交")
    await _run_before_auto_send(before_auto_send)
    if not await click_phone_submit(page, phone_input):
        raise RuntimeError("手机号页: 未找到提交按钮")
    logger("手机号页: 已点击提交，等待跳转并发送短信验证码")
    await page.wait_for_timeout(2000)


async def find_sms_code_input(page: Page) -> Locator | None:
    selectors = (
        'input[name="code"]',
        'input[autocomplete="one-time-code"]',
        'input[inputmode="numeric"]',
        'input[placeholder*="验证码"]',
        'input[placeholder*="code" i]',
        'input[type="tel"]',
        'input[type="text"]',
        'input[type="number"]',
    )
    for selector in selectors:
        locators = page.locator(selector)
        for index in range(min(await locators.count(), 8)):
            locator = locators.nth(index)
            try:
                if not await locator.is_visible(timeout=400):
                    continue
                name = await locator.get_attribute("name", timeout=400) or ""
                if name in {"phoneNumberInput", "local_number", "country_code"}:
                    continue
                return locator
            except Exception:
                continue
    return None


async def page_is_phone_verification_rate_limited(page: Page) -> bool:
    text = await body_text(page)
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


async def page_looks_like_sms_verification(page: Page) -> bool:
    lower_url = (page.url or "").lower()
    if "contact-verification" in lower_url or "phone-verification" in lower_url:
        return True
    if "radar-challenge" in lower_url and "/send" not in lower_url:
        return True
    text = (await body_text(page)).lower()
    return bool(await find_sms_code_input(page) and any(hint in text for hint in ("sms", "text message", "verification code", "验证码", "短信")))


async def wait_for_sms_verification_page(
    page: Page,
    logger: Callable[[str], None],
    timeout: int = 45,
) -> str:
    """等待短信验证码页。返回 sms | password | rate_limited | timeout。"""
    for index in range(max(1, timeout)):
        if await page_is_phone_verification_rate_limited(page):
            logger(
                "手机号页: 检测到「此手机号码的验证请求过多，请联系您的管理员」，"
                "未进入短信验证码页"
            )
            return "rate_limited"
        if await page_looks_like_create_password(page):
            logger(f"手机号页: 当前是创建密码页，不是短信验证码页 url={short_url(page.url)}")
            return "password"
        if await page_looks_like_sms_verification(page):
            logger("手机号页: 页面已进入短信验证码阶段")
            return "sms"
        if index == 0 or (index + 1) % 5 == 0:
            logger(f"手机号页: 等待短信验证码输入页出现... url={short_url(page.url)}")
        await page.wait_for_timeout(1000)
    return "timeout"


async def page_looks_like_create_password(page: Page) -> bool:
    lower_url = (page.url or "").lower()
    if "create-account/password" in lower_url or "/password" in lower_url:
        return await visible_input_count(page, r"password") > 0
    text = await body_text(page)
    low = text.lower()
    return await visible_input_count(page, r"password") > 0 and any(
        hint in low or hint in text
        for hint in ("create password", "创建密码", "设置密码")
    )


async def fill_create_password_page(page: Page, password: str, logger: Callable[[str], None]) -> None:
    locator = await first_visible(page.locator("input[type='password']"))
    if not locator:
        raise RuntimeError("创建密码页未找到密码输入框")
    await human_fill(locator, password, force_mouse=True)
    logger("手机号页: 注册密码已填入，点击继续")
    if not await click_phone_submit(page, locator):
        raise RuntimeError("创建密码页继续按钮点击失败")
    await page.wait_for_timeout(3000)


async def fill_sms_code(page: Page, code: str, logger: Callable[[str], None]) -> None:
    from .utils import extract_code

    digits = extract_code(code) or re.sub(r"\D+", "", code or "")
    if not digits:
        raise RuntimeError(f"无法从短信提取验证码: {code}")
    code_input = await find_sms_code_input(page)
    if not code_input:
        raise RuntimeError("未找到短信验证码输入框")
    logger(f"手机号页: 已找到短信验证码输入框，填入验证码 {digits}")
    await human_fill(code_input, digits, force_mouse=True)
    logger("手机号页: 短信验证码已填入页面")
    if not await click_phone_submit(page, code_input):
        raise RuntimeError("短信验证码提交按钮点击失败")
    logger("手机号页: 已点击验证码继续/提交按钮")
    await page.wait_for_timeout(3000)


async def wait_for_code_submit_result(page: Page, timeout: int = 12) -> tuple[str, str]:
    for _ in range(max(1, timeout * 2)):
        err = await detect_otp_error(page)
        if err:
            return "invalid", err
        if not await find_sms_code_input(page):
            return "accepted", ""
        await page.wait_for_timeout(500)
    err = await detect_otp_error(page)
    if err:
        return "invalid", err
    return "pending", ""


async def detect_otp_error(page: Page) -> str:
    low = (await body_text(page)).lower().replace("\n", " ")
    for hint in (
        "invalid code",
        "incorrect code",
        "wrong code",
        "expired code",
        "check the code and try again",
        "验证码无效",
        "验证码错误",
        "验证码已过期",
    ):
        if hint in low:
            return hint
    return ""


async def current_phone_country_code(page: Page) -> str:
    try:
        return str(
            await page.evaluate(
                r"""() => {
                    for (const node of document.querySelectorAll('button, select')) {
                        const text = (node.innerText || node.textContent || '').trim();
                        const match = text.match(/\+(\d+)/);
                        if (match) return match[1];
                    }
                    return '';
                }"""
            )
            or ""
        )
    except Exception:
        return ""


async def maybe_visible_selector(page: Page, selector: str, timeout: int = 1000) -> Locator | None:
    deadline = asyncio.get_running_loop().time() + timeout / 1000
    while asyncio.get_running_loop().time() < deadline:
        for frame in [page.main_frame, *[frame for frame in page.frames if frame != page.main_frame]]:
            try:
                locator = frame.locator(selector).first
                if await locator.is_visible(timeout=250):
                    return locator
            except Exception:
                continue
        await page.wait_for_timeout(100)
    return None


def short_error_text(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0][:120] if str(exc).strip() else exc.__class__.__name__


def is_page_closed_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "target page" in text and ("closed" in text or "has been closed" in text)


def short_url(url: str, limit: int = 90) -> str:
    if len(url) <= limit:
        return url
    return url[: limit - 3] + "..."


async def body_text(page: Page) -> str:
    try:
        return await page.locator("body").inner_text(timeout=3000)
    except PlaywrightTimeoutError:
        return ""


def likely_code_page(text: str) -> bool:
    return any(
        key in text
        for key in [
            "verification code",
            "enter code",
            "one-time code",
            "otp",
            "check your email",
            "verify your email",
        ]
    )


async def is_invalid_code_page(page: Page) -> bool:
    text = await body_text(page)
    low = text.lower()
    return any(
        key in low
        for key in [
            "invalid code",
            "incorrect code",
            "code is incorrect",
            "wrong code",
            "try again",
            "代码不正确",
            "验证码不正确",
            "无效代码",
        ]
    )


def recoverable_auth_error_reason(low: str, text: str) -> str:
    haystack = f"{low} {text}".lower()
    for hint in AUTH_RETRY_ERROR_HINTS:
        if hint.lower() not in haystack:
            continue
        if hint.lower() in {"something went wrong", "oops, an error occurred"}:
            if not any(k.lower() in haystack for k in AUTH_RETRY_BUTTON_TEXTS):
                continue
        return hint
    return ""


def is_fatal_account_error(low: str, text: str) -> bool:
    return any(
        key in low
        for key in [
            "max_check_attempts",
            "too many attempts",
            "verification failed",
        ]
    ) or any(key in text for key in ["验证过程中出错", "糟糕，出错了", "请重试"])


async def click_auth_retry_button(page: Page) -> bool:
    selectors = [
        'button:has-text("重试")',
        'button:has-text("再试一次")',
        '[role="button"]:has-text("重试")',
        '[role="button"]:has-text("再试一次")',
        'button:has-text("Retry")',
        '[role="button"]:has-text("Retry")',
        'button:has-text("Try again")',
        '[role="button"]:has-text("Try again")',
        '[aria-label*="重试"]',
        '[title*="重试"]',
        'input[type="button"][value*="重试"]',
        'input[type="submit"][value*="重试"]',
    ]
    for selector in selectors:
        try:
            items = page.locator(selector)
            count = min(await items.count(), 6)
            for index in range(count - 1, -1, -1):
                item = items.nth(index)
                if await item.is_visible(timeout=500) and await item.is_enabled(timeout=500):
                    await item.click(timeout=3000)
                    return True
        except Exception:
            continue
    try:
        button = page.get_by_role("button", name=re.compile(r"^(重试|再试一次|Retry|Try again)$", re.I)).last
        if await button.is_visible(timeout=1000) and await button.is_enabled(timeout=1000):
            await button.click(timeout=3000)
            return True
    except Exception:
        pass
    try:
        clicked = await page.evaluate(
            """() => {
                const visible = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < 10 || r.height < 10) return false;
                    const s = window.getComputedStyle(el);
                    return s && s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity || '1') > 0.05;
                };
                const keys = ['重试', '再试一次', 'retry', 'try again'];
                const nodes = Array.from(document.querySelectorAll(
                    'button, [role="button"], a, input[type="button"], input[type="submit"], div[role="button"], span'
                ));
                for (let i = nodes.length - 1; i >= 0; i--) {
                    const el = nodes[i];
                    if (!visible(el)) continue;
                    const t = ((el.innerText || el.textContent || el.value || '') + '').toLowerCase().replace(/\\s+/g, ' ').trim();
                    if (!t || !keys.some((k) => t.includes(k))) continue;
                    const target = el.closest('button,[role="button"],a,input[type="button"],input[type="submit"],div[role="button"]') || el;
                    try {
                        target.click();
                        return true;
                    } catch {}
                }
                return false;
            }"""
        )
        return bool(clicked)
    except Exception:
        return False


async def fatal_error_message(page: Page) -> str:
    text = await body_text(page)
    if "max_check_attempts" in text:
        return "验证码检查次数已达上限(max_check_attempts)，跳过当前账号"
    return "账号注册进入不可恢复错误页，跳过当前账号"


async def dump_unknown_page(page: Page, index: int) -> None:
    try:
        out = Path(__file__).resolve().parents[1] / "output" / "debug"
        out.mkdir(parents=True, exist_ok=True)
        text = await body_text(page)
        (out / f"unknown_{index}.txt").write_text(
            f"URL: {page.url}\n\n{text[:4000]}",
            encoding="utf-8",
        )
        await page.screenshot(path=str(out / f"unknown_{index}.png"), full_page=True)
    except Exception:
        pass


def is_entry_page(low: str, text: str) -> bool:
    return (
        ("开始使用" in text and "登录" in text)
        or ("get started" in low and ("log in" in low or "sign up" in low))
        or ("免费注册" in text and "登录" in text)
        or ("404" in text and "找不到页面" in text and "登录" in text)
    )


async def chatgpt_logged_in_markers(page: Page, low: str, text: str) -> bool:
    if "auth/login" in (page.url or "").lower() or "auth/signup" in (page.url or "").lower():
        return False
    if "log in" in low or "sign up" in low or "登录" in text or "注册" in text:
        return False
    selectors = (
        "textarea[placeholder]",
        "textarea[data-testid]",
        "[data-testid='composer-speech-button']",
        "[data-testid='send-button']",
        "button[aria-label*='Send' i]",
        "button[aria-label*='发送']",
    )
    for selector in selectors:
        try:
            if await page.locator(selector).first.is_visible(timeout=300):
                return True
        except Exception:
            continue
    return any(
        hint in low or hint in text
        for hint in [
            "message chatgpt",
            "what can i help with",
            "准备好了，随时开始",
            "有问题，尽管问",
            "有什么可以帮忙",
        ]
    )


async def is_phone_login_page(page: Page, low: str, text: str) -> bool:
    url = page.url.lower()
    if "usernamekind=phone_number" in url or "screen_hint=phone" in url:
        return True

    email_inputs = await visible_input_count(page, r"email|username")
    if email_inputs > 0:
        return False

    phone_inputs = await visible_input_count(page, r"phone|tel|电话号码|手机号")
    if phone_inputs > 0 and (
        "电话号码" in text
        or "手机号" in text
        or "phone number" in low
        or "mobile number" in low
    ):
        return True

    return (
        ("继续使用电子邮件地址登录" in text and ("电话号码" in text or "手机号" in text))
        or ("continue with email" in low and ("phone number" in low or "mobile number" in low))
    )


async def visible_input_count(page: Page, attr_pattern: str) -> int:
    pattern = re.compile(attr_pattern, flags=re.I)
    inputs = await visible_locators(page.locator("input:not([type='file']), textarea"))
    count = 0
    for item in inputs:
        attrs = await input_attrs(item)
        hay = " ".join(attrs.values())
        if pattern.search(hay):
            count += 1
    return count


async def visible_code_inputs(page: Page) -> int:
    inputs = await visible_locators(page.locator("input:not([type='file'])"))
    count = 0
    for item in inputs:
        attrs = await input_attrs(item)
        hay = " ".join(attrs.values()).lower()
        if any(k in hay for k in ["code", "otp", "one-time", "verification"]):
            count += 1
    return count


async def visible_locators(locator: Locator) -> list[Locator]:
    result = []
    for index in range(await locator.count()):
        item = locator.nth(index)
        try:
            if await item.is_visible() and await item.is_enabled():
                input_type = (await item.get_attribute("type") or "").lower()
                if input_type != "file":
                    result.append(item)
        except Exception:
            continue
    return result


async def first_visible(locator: Locator) -> Locator | None:
    items = await visible_locators(locator)
    return items[0] if items else None


async def first_textbox(page: Page) -> Locator | None:
    return await first_visible(page.locator("input:not([type='file']), textarea"))


async def input_attrs(locator: Locator) -> dict[str, str]:
    keys = ["type", "name", "id", "placeholder", "autocomplete", "aria-label", "data-testid"]
    values: dict[str, str] = {}
    for key in keys:
        try:
            value = await locator.get_attribute(key)
            if value:
                values[key] = value
        except Exception:
            pass
    return values


async def human_fill(locator: Locator, value: str, force_mouse: bool = False) -> None:
    await locator.scroll_into_view_if_needed()
    if force_mouse:
        box = await locator.bounding_box()
        if box:
            await locator.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        else:
            await locator.click(force=True)
    else:
        await locator.click(force=True)
    await locator.page.keyboard.press("Control+A")
    await locator.page.keyboard.press("Backspace")
    await locator.page.keyboard.type(str(value), delay=25)
    await locator.evaluate(
        """(el, value) => {
            if ((el.value || '').trim() !== String(value)) {
                const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                if (setter) setter.call(el, value);
                else el.value = value;
            }
            el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: String(value) }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        str(value),
    )


async def fill_profile_by_js(page: Page, full_name: str, age: str, logger: Callable[[str], None] | None = None) -> bool:
    result = await page.evaluate(
        """({ fullName, age }) => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
            };
            const text = (node) => (node?.innerText || node?.textContent || '').replace(/\\s+/g, ' ').trim();
            const labelFor = (el) => {
                const bits = [
                    el.name, el.id, el.type, el.inputMode, el.placeholder, el.autocomplete,
                    el.getAttribute('aria-label'), el.getAttribute('data-testid')
                ];
                if (el.id) {
                    const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                    if (label) bits.push(text(label));
                }
                return bits.filter(Boolean).join(' ');
            };
            const setValue = (el, value) => {
                el.scrollIntoView({ block: 'center', inline: 'nearest' });
                const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                if (setter) setter.call(el, value);
                else el.value = value;
                el.setAttribute('value', value);
                el.focus();
                el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Tab', code: 'Tab' }));
                el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'Tab', code: 'Tab' }));
            };
            const fields = [...document.querySelectorAll('input:not([type=file]), textarea')]
                .filter((el) => visible(el) && !el.disabled && !el.readOnly)
                .filter((el) => !/email|password|hidden|checkbox|radio|submit|button/i.test(el.type || ''));
            let nameEl = null;
            let ageEl = null;
            for (const el of fields) {
                const meta = labelFor(el);
                if (!ageEl && (/\\bage\\b|年龄|birthday|birth|year/i.test(meta) || /number|numeric|tel/i.test([el.type, el.inputMode].join(' ')))) ageEl = el;
            }
            for (const el of fields) {
                if (el === ageEl) continue;
                const meta = labelFor(el);
                if (!nameEl && /全名|姓名|名字|full\\s*name|name/i.test(meta)) nameEl = el;
            }
            if (!nameEl) nameEl = fields[0] || null;
            if (!ageEl) {
                ageEl = fields.find((el) => el !== nameEl && /number|numeric|tel/i.test([el.type, el.inputMode].join(' '))) || null;
            }
            if (!ageEl) ageEl = fields.find((el) => el !== nameEl) || null;
            for (const el of fields) {
                if (el === nameEl || el === ageEl) continue;
            }
            if (nameEl) setValue(nameEl, fullName);
            if (ageEl) setValue(ageEl, age);
            return {
                name: Boolean(nameEl),
                age: Boolean(ageEl),
                valid: Boolean(nameEl && ageEl && !/^\\d+$/.test(String(nameEl.value || '').trim()) && /^\\d+$/.test(String(ageEl.value || '').trim())),
                fields: fields.map((el) => ({ value: el.value, meta: labelFor(el).slice(0, 120) }))
            };
        }""",
        {"fullName": full_name, "age": age},
    )
    if result.get("name") and result.get("age") and result.get("valid"):
        (logger or log)("资料页已通过稳态填充写入姓名和年龄")
        return True
    (logger or log)(f"资料页稳态填充未完整命中: {result}")
    return False


async def click_profile_submit_by_js(page: Page) -> None:
    clicked = await click_submit_by_js(page, ["完成帐户创建", "完成账户创建", "完成帐户建立", "完成账户建立", "完成", "创建", "Continue", "Done", "Create"])
    if not clicked:
        await click_continue(page, profile=True)


async def click_submit_by_js(page: Page, labels: list[str]) -> bool:
    clicked = await page.evaluate(
        """(labels) => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
            };
            const text = (el) => (el?.innerText || el?.textContent || '').replace(/\\s+/g, ' ').trim();
            const meta = (el) => [
                text(el),
                el?.value || '',
                el?.getAttribute?.('aria-label') || '',
                el?.getAttribute?.('title') || '',
                el?.getAttribute?.('data-testid') || '',
                el?.getAttribute?.('data-provider') || '',
                el?.outerHTML || ''
            ].join(' ').toLowerCase();
            const isSwitchEmail = (value) => /继续使用电子邮件地址登录|continue with email/i.test(value || '');
            const buttons = [...document.querySelectorAll('button, [role="button"], input[type=submit]')]
                .filter((el) => visible(el) && !el.disabled)
                .filter((el) => {
                    const value = text(el) || el.value || '';
                    const hay = meta(el);
                    if (/google|apple|microsoft|github|sso|oauth|social|provider/i.test(hay)) return false;
                    if (isSwitchEmail(value)) return false;
                    return true;
                });
            const labeled = buttons.filter((el) => (text(el) || el.value || '').trim());
            const wanted = labels.map((s) => String(s).toLowerCase());
            const exact = labeled.find((el) => {
                const value = (text(el) || el.value || '').toLowerCase();
                return wanted.some((label) => value === label);
            });
            const activate = (target) => {
                target.scrollIntoView({ block: 'center', inline: 'nearest' });
                target.focus?.();
                target.click();
                const form = target.closest?.('form') || document.querySelector('form');
                if (form && typeof form.requestSubmit === 'function') {
                    setTimeout(() => {
                        try { form.requestSubmit(target instanceof HTMLButtonElement ? target : undefined); } catch (e) {}
                    }, 50);
                }
            };
            if (exact) {
                activate(exact);
                return true;
            }
            const primary = labeled.find((el) => {
                const value = (text(el) || el.value || '').toLowerCase();
                return wanted.some((label) => value === label || value.includes(label));
            });
            const target = primary || labeled[labeled.length - 1];
            if (!target) return false;
            activate(target);
            return true;
        }""",
        labels,
    )
    if clicked:
        await page.keyboard.press("Enter")
        await settle(page)
        return True
    return False


async def click_email_submit(page: Page, email_input: Locator) -> bool:
    clicked = await page.evaluate(
        """(input) => {
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
            };
            const label = (el) => (el?.innerText || el?.textContent || el?.value || '').replace(/\\s+/g, ' ').trim();
            const meta = (el) => [
                label(el),
                el?.getAttribute?.('aria-label') || '',
                el?.getAttribute?.('title') || '',
                el?.getAttribute?.('data-testid') || '',
                el?.getAttribute?.('data-provider') || '',
                el?.outerHTML || ''
            ].join(' ').toLowerCase();
            const isSocial = (el) => /google|apple|microsoft|github|sso|oauth|social|provider/.test(meta(el));
            const isWanted = (el) => /^(continue|next|submit|log in|sign in|sign up|create|继续|下一步|登录|注册)$/i.test(label(el))
                || /continue|next|继续|下一步/.test(label(el).toLowerCase());
            const activate = (target) => {
                target.scrollIntoView({ block: 'center', inline: 'nearest' });
                target.focus?.();
                target.click();
                const form = target.closest?.('form') || input.closest?.('form');
                if (form && typeof form.requestSubmit === 'function') {
                    setTimeout(() => {
                        try { form.requestSubmit(target instanceof HTMLButtonElement ? target : undefined); } catch (e) {}
                    }, 50);
                }
            };
            const form = input.closest('form');
            const scopes = [
                form,
                input.closest('section'),
                input.closest('main'),
                input.closest('[role="main"]'),
                input.closest('div')
            ].filter(Boolean);
            for (const scope of scopes) {
                const buttons = [...scope.querySelectorAll('button, input[type=submit]')]
                    .filter((el) => visible(el) && !el.disabled && !isSocial(el));
                const wanted = buttons.find(isWanted);
                if (wanted) {
                    activate(wanted);
                    return { clicked: true, mode: 'button', label: label(wanted) };
                }
                if (form && scope === form && buttons.length === 1) {
                    activate(buttons[0]);
                    return { clicked: true, mode: 'single-form-button', label: label(buttons[0]) };
                }
            }
            input.focus();
            input.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Enter', code: 'Enter' }));
            input.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'Enter', code: 'Enter' }));
            if (form && typeof form.requestSubmit === 'function') {
                try {
                    form.requestSubmit();
                    return { clicked: true, mode: 'form-enter' };
                } catch (e) {}
            }
            return { clicked: false, mode: 'not-found' };
        }""",
        await email_input.element_handle(),
    )
    if isinstance(clicked, dict) and clicked.get("clicked"):
        await page.keyboard.press("Enter")
        await settle(page)
        return True
    return False


async def click_continue(page: Page, profile: bool = False, anchor: Locator | None = None) -> None:
    patterns = [
        re.compile(r"^(continue|next|submit|verify|log in|sign up|create|finish|done)$", re.I),
        re.compile(r"^(继续|下一步|验证|登录|注册|完成|创建)$"),
    ]
    if profile:
        patterns.insert(0, re.compile(r"continue|finish|done|create", re.I))
        patterns.insert(1, re.compile(r"完成|创建|继续"))
    if anchor:
        form_button = await find_submit_near_anchor(anchor)
        if form_button:
            await form_button.click()
            await settle(page)
            return
    for pattern in patterns:
        buttons = page.get_by_role("button", name=pattern)
        for index in range(await buttons.count()):
            button = buttons.nth(index)
            try:
                if await button.is_visible() and await button.is_enabled() and not await is_social_oauth(button):
                    await button.click()
                    await settle(page)
                    return
            except Exception:
                continue
    candidates = await visible_locators(page.locator("button, input[type='submit']"))
    candidates = [item for item in candidates if not await is_social_oauth(item)]
    if candidates:
        await candidates[0].click()
        await settle(page)
        return
    await page.keyboard.press("Enter")
    await settle(page)


async def click_by_visible_text(page: Page, label: str) -> bool:
    candidates = [
        page.locator(f"text={label}"),
        page.locator("button, a, [role='button']").filter(has_text=label),
    ]
    for locator in candidates:
        for index in range(await locator.count()):
            item = locator.nth(index)
            try:
                if await item.is_visible():
                    if await is_social_oauth(item):
                        continue
                    await item.click()
                    return True
            except Exception:
                continue
    return False


async def settle(page: Page) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(700)


async def find_submit_near_anchor(anchor: Locator) -> Locator | None:
    try:
        form = anchor.locator("xpath=ancestor::form[1]")
        if await form.count():
            buttons = await visible_locators(form.locator("button, input[type='submit']"))
            clean = [button for button in buttons if not await is_social_oauth(button)]
            if clean:
                return clean[-1]
    except Exception:
        pass
    try:
        nearby = anchor.locator(
            "xpath=ancestor::*[self::form or self::main or self::section or self::div][1]//button[not(.//*[contains(translate(., 'GOOGLEMICROSOFTAPPLE', 'googlemicrosoftapple'), 'google')])]"
        )
        buttons = await visible_locators(nearby)
        clean = [button for button in buttons if not await is_social_oauth(button)]
        if clean:
            return clean[-1]
    except Exception:
        pass
    return None


async def is_social_oauth(locator: Locator) -> bool:
    try:
        text = await locator.inner_text(timeout=1000)
    except Exception:
        text = ""
    attrs = await input_attrs(locator)
    try:
        html = await locator.evaluate("(el) => el.outerHTML || ''")
    except Exception:
        html = ""
    hay = f"{text} {' '.join(attrs.values())} {html}".lower()
    return any(key in hay for key in ["google", "apple", "microsoft", "github", "sso", "oauth", "social", "provider"])
