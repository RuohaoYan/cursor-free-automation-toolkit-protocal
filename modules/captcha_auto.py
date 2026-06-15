"""安全验证：优先浏览器点击，可选 CapSolver 打码。"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from .utils import load_env, log

LogFn = Callable[[str], None]

_SECURITY_CHALLENGE_HINTS = (
    "just a moment",
    "checking your browser",
    "verify you are human",
    "human verification",
    "are you human",
    "confirm you are human",
    "prove you are human",
    "security verification",
    "security service",
    "please stand by",
    "正在进行安全验证",
    "安全验证",
    "验证您不是自动程序",
    "本网站使用安全服务",
    "恶意自动程序",
    "请完成以下操作",
    "确认您是真人",
    "确认你是真人",
    "请验证您是真人",
    "请验证你是真人",
    "验证您是真人",
    "验证你是真人",
    "cf-browser-verification",
)

_TURNSTILE_IFRAME_SELECTORS = (
    'iframe[src*="challenges.cloudflare.com"]',
    'iframe[src*="turnstile"]',
    'iframe[title*="Cloudflare" i]',
    'iframe[title*="challenge" i]',
    'iframe[title*="安全" i]',
)

_CLICK_TARGETS = (
    ".cb-lb",
    "label.cb-lb",
    ".cb-i",
    'label:has(input[type="checkbox"])',
    "#checkbox",
    'input[type="checkbox"]:not([disabled])',
    ".ctp-checkbox-label",
    "label.ctp-checkbox-label",
    '[role="checkbox"]',
    '[role="button"][aria-label*="verify" i]',
    '[role="button"][aria-label*="验证" i]',
)

_CLOUDFLARE_WIDGET_SELECTORS = (
    ".cb-lb",
    "label.cb-lb",
    ".cb-i",
    'label:has(input[type="checkbox"])',
    "#checkbox",
    'input[type="checkbox"]:not([disabled])',
)


def _captcha_mode() -> str:
    env = load_env(".env")
    return (
        env.get("CURSOR_CAPTCHA_MODE")
        or env.get("FREE_CAPTCHA_MODE")
        or env.get("CAPTCHA_MODE")
        or "auto"
    ).strip().lower()


def _capsolver_key() -> str:
    env = load_env(".env")
    return (env.get("CAPSOLVER_API_KEY") or env.get("CAPSOLVER_KEY") or "").strip()


def should_try_click() -> bool:
    """是否尝试在浏览器内点击验证框（默认开启，无需打码平台）。"""
    mode = _captcha_mode()
    return mode not in {"manual", "api"}


def should_use_capsolver() -> bool:
    """是否使用 CapSolver API（需配置 CAPSOLVER_API_KEY）。"""
    mode = _captcha_mode()
    if mode == "manual" or mode == "click":
        return False
    if mode == "api":
        return bool(_capsolver_key())
    return bool(_capsolver_key())


def should_use_local_solver() -> bool:
    """是否使用本地 Turnstile Solver（any-auto-register / Theyka 兼容 API）。"""
    from .turnstile_solver_client import is_local_solver_configured

    mode = _captcha_mode()
    if mode == "manual":
        return False
    if not is_local_solver_configured():
        return False
    return mode in {"auto", "solver", "local", "api"}


def should_auto_solve() -> bool:
    """兼容旧逻辑：仅表示「配置了打码且模式允许 API」。"""
    return should_use_capsolver()


async def turnstile_token_ready(page: Any) -> bool:
    try:
        return bool(
            await page.evaluate(
                """() => {
                    const t = document.querySelector(
                        '[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
                    );
                    return !!(t && String(t.value || '').length > 20);
                }"""
            )
        )
    except Exception:
        return False


async def _challenge_navigated_past_cf(page: Any) -> bool:
    try:
        url = (page.url or "").lower()
    except Exception:
        return False
    if "__cf_chl_rt_tk" in url or "__cf_chl" in url:
        return False
    if "authenticator.cursor" in url:
        try:
            still_challenging = await page.evaluate(
                """() => {
                    const text = (document.body?.innerText || '').toLowerCase();
                    const hints = [
                        'just a moment',
                        'checking your browser',
                        'verify you are human',
                        '正在进行安全验证',
                        '安全验证',
                        'confirm you are human',
                    ];
                    if (hints.some((h) => text.includes(h))) return true;
                    return !!document.querySelector(
                        'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]'
                    );
                }"""
            )
            if still_challenging:
                return False
        except Exception:
            pass
        return True
    if "cursor.com" in url and "challenge" not in url:
        return True
    return False


async def _wait_challenge_clear(page: Any, *, seconds: int = 20) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if await turnstile_token_ready(page):
            await _try_submit_cf_challenge(page)
            await asyncio.sleep(1.0)
        visible, _ = await security_challenge_visible(page)
        if not visible:
            return True
        await asyncio.sleep(0.25)
    return False


async def _try_submit_cf_challenge(page: Any) -> None:
    try:
        await page.evaluate(
            """() => {
                const token = document.querySelector(
                    '[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
                );
                if (!token || String(token.value || '').length < 20) return;
                const form = token.closest('form');
                if (form) {
                    try { form.requestSubmit(); } catch { form.submit(); }
                }
            }"""
        )
    except Exception:
        pass


async def security_challenge_visible(page: Any) -> tuple[bool, str]:
    try:
        if await turnstile_token_ready(page):
            if await _challenge_navigated_past_cf(page):
                return False, ""
            return True, "turnstile-token-pending"
    except Exception:
        pass

    try:
        # 注册/登录表单已可交互时，忽略页面上残留的 CF 文案或 iframe
        form_ready = await page.evaluate(
            """() => {
                const visible = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        st.visibility !== 'hidden' && st.display !== 'none' && !el.disabled;
                };
                const email = document.querySelector(
                    'input[type="email"], input[name="email"], input[name="emailAddress"]'
                );
                if (email && visible(email)) {
                    const form = email.closest('form');
                    if (form) {
                        const submit = form.querySelector('button[type="submit"], button:not([type="button"])');
                        if (submit && visible(submit)) return true;
                    }
                    const buttons = [...document.querySelectorAll('button, [role="button"]')];
                    if (buttons.some((btn) => visible(btn))) return true;
                    return true;
                }
                const token = document.querySelector('[name="cf-turnstile-response"]');
                if (token && String(token.value || '').length > 20) {
                    const email2 = document.querySelector('input[type="email"], input[name="email"]');
                    if (email2 && visible(email2)) return true;
                }
                return false;
            }"""
        )
        if form_ready:
            return False, ""
    except Exception:
        pass

    try:
        title = (await page.title()).lower()
        text = (await page.evaluate("() => (document.body?.innerText || '').toLowerCase()"))[:4000]
    except Exception:
        return False, ""

    if "cloudflare" in title:
        return True, "cloudflare-title"

    for hint in _SECURITY_CHALLENGE_HINTS:
        if hint in title or hint in text:
            return True, hint

    try:
        has_frame = await page.evaluate(
            """() => {
                const selectors = [
                    'iframe[src*="challenges.cloudflare.com"]',
                    'iframe[src*="turnstile"]',
                    'iframe[src*="hcaptcha"]',
                    'iframe[src*="recaptcha"]',
                    '#challenge-running',
                    '#cf-challenge-running',
                ];
                return selectors.some((s) => document.querySelector(s));
            }"""
        )
        if has_frame:
            return True, "challenge-iframe"
    except Exception:
        pass

    return False, ""


async def extract_turnstile_info(page: Any) -> dict[str, str]:
    try:
        info = await page.evaluate(
            """() => {
                const out = { sitekey: '', pageurl: location.href, provider: 'turnstile' };
                const widget = document.querySelector('[data-sitekey], .cf-turnstile');
                if (widget) {
                    out.sitekey = widget.getAttribute('data-sitekey') || '';
                }
                const iframe = document.querySelector(
                    'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]'
                );
                if (iframe && iframe.src) {
                    const m = iframe.src.match(/[/?&]k=([^&]+)/);
                    if (m) out.sitekey = decodeURIComponent(m[1]);
                }
                if (!out.sitekey) {
                    const scripts = Array.from(document.querySelectorAll('script[src*="turnstile"]'));
                    for (const s of scripts) {
                        const m = String(s.src || '').match(/[/?&]render=([^&]+)/);
                        if (m) { out.sitekey = decodeURIComponent(m[1]); break; }
                    }
                }
                return out;
            }"""
        )
        if isinstance(info, dict):
            return {
                "sitekey": str(info.get("sitekey") or "").strip(),
                "pageurl": str(info.get("pageurl") or page.url or "").strip(),
                "provider": "turnstile",
            }
    except Exception:
        pass
    return {"sitekey": "", "pageurl": str(page.url or ""), "provider": "turnstile"}


async def extract_recaptcha_info(page: Any) -> dict[str, str]:
    try:
        info = await page.evaluate(
            """() => {
                const frames = Array.from(document.querySelectorAll('iframe'))
                    .map((f) => String(f.getAttribute('src') || ''));
                const rec = frames.find((s) => /recaptcha/.test(s));
                if (!rec) return { provider: 'unknown', sitekey: '', pageurl: location.href, version: 'v2' };
                const key = (rec.match(/[/?&]k=([^&]+)/) || [])[1] || '';
                const isV3 = /recaptcha_v3|render=explicit/.test(rec);
                return {
                    provider: 'recaptcha',
                    sitekey: key ? decodeURIComponent(key) : '',
                    pageurl: location.href,
                    version: isV3 ? 'v3' : 'v2',
                };
            }"""
        )
        if isinstance(info, dict) and info.get("sitekey"):
            return {
                "provider": "recaptcha",
                "sitekey": str(info["sitekey"]),
                "pageurl": str(info.get("pageurl") or page.url),
                "version": str(info.get("version") or "v2"),
            }
    except Exception:
        pass
    return {"provider": "unknown", "sitekey": "", "pageurl": str(page.url or ""), "version": "v2"}


async def _click_locator_candidate(loc: Any, *, timeout: int = 4000, force: bool = True) -> bool:
    try:
        if await loc.count() <= 0:
            return False
    except Exception:
        pass

    try:
        if not await loc.is_visible(timeout=800):
            return False
    except TypeError:
        try:
            if not await loc.is_visible():
                return False
        except Exception:
            pass
    except Exception:
        pass

    try:
        box = await loc.bounding_box(timeout=800)
        if not box or box.get("width", 0) < 6 or box.get("height", 0) < 6:
            return False
    except TypeError:
        try:
            box = await loc.bounding_box()
            if not box or box.get("width", 0) < 6 or box.get("height", 0) < 6:
                return False
        except Exception:
            pass
    except Exception:
        pass

    try:
        await loc.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass

    await loc.click(timeout=timeout, force=force)
    return True


async def _click_in_frame(frame: Any, say: LogFn) -> bool:
    failures: list[str] = []
    for sel in _CLICK_TARGETS:
        try:
            loc = frame.locator(sel).first
            if await _click_locator_candidate(loc):
                say(f"[Captcha] 已点击验证框: {sel}")
                return True
        except Exception as exc:
            failures.append(f"{sel}={exc.__class__.__name__}:{str(exc)[:80]}")
            continue
    if failures:
        say(f"[Captcha] frame 内候选控件点击失败: {' | '.join(failures[:3])}")
    return False


async def _frame_has_cloudflare_widget(frame: Any) -> bool:
    try:
        return bool(
            await frame.evaluate(
                """() => !!document.querySelector(
                    '.cb-lb, label.cb-lb, .cb-i, label input[type="checkbox"], input[type="checkbox"], #checkbox'
                )"""
            )
        )
    except Exception:
        return False


async def _click_cloudflare_widget_dom(frame: Any, say: LogFn) -> bool:
    """Click Cloudflare Turnstile checkbox DOM inside its own frame."""
    for sel in _CLOUDFLARE_WIDGET_SELECTORS:
        try:
            loc = frame.locator(sel).first
            if await _click_locator_candidate(loc, timeout=5000, force=True):
                say(f"[Captcha] 已点击 Cloudflare 验证控件: {sel}")
                return True
        except Exception as exc:
            say(f"[Captcha] Cloudflare 控件点击失败 {sel}: {exc.__class__.__name__}: {str(exc)[:120]}")
            continue

    try:
        clicked = await frame.evaluate(
            """() => {
                const selectors = [
                    '.cb-lb',
                    'label.cb-lb',
                    '.cb-i',
                    'label input[type="checkbox"]',
                    'input[type="checkbox"]',
                    '#checkbox'
                ];
                const visible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 2 && rect.height > 2 &&
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        Number(style.opacity || '1') > 0.01;
                };
                for (const selector of selectors) {
                    const el = document.querySelector(selector);
                    if (!el || !visible(el)) continue;
                    const target = el.closest('label,button,[role="button"],[role="checkbox"]') || el;
                    target.scrollIntoView({ block: 'center', inline: 'center' });
                    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                        target.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
                    }
                    if (target !== el) {
                        el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                    }
                    return selector;
                }
                return '';
            }"""
        )
        if clicked:
            say(f"[Captcha] 已通过 DOM 事件点击 Cloudflare 验证控件: {clicked}")
            return True
    except Exception as exc:
        say(f"[Captcha] Cloudflare DOM 事件点击失败: {exc.__class__.__name__}: {str(exc)[:120]}")
    return False


async def _click_iframe_widget_area(page: Any, iframe_sel: str, say: LogFn) -> bool:
    try:
        iframe = page.locator(iframe_sel).first
        if await iframe.count() <= 0:
            return False
        visible = True
        try:
            visible = await iframe.is_visible(timeout=800)
        except TypeError:
            visible = await iframe.is_visible()
        except Exception:
            visible = False

        box = await iframe.bounding_box()
        if not box:
            try:
                box = await iframe.evaluate(
                    """(el) => {
                        const rect = el.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) return null;
                        return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
                    }"""
                )
            except Exception:
                box = None
        if not box:
            return False
        width = float(box.get("width", 0))
        height = float(box.get("height", 0))
        if width < 20 or height < 15 or width > 900 or height > 320:
            return False

        try:
            await iframe.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass

        offset_x = min(max(width * 0.18, 24), max(width / 2, 24))
        await page.mouse.click(float(box["x"]) + offset_x, float(box["y"]) + height / 2)
        say(
            "[Captcha] 已点击 iframe 验证框区域"
            + ("" if visible else "（无头不可见坐标点击）")
        )
        return True
    except Exception:
        return False


async def _click_turnstile_like_real_browser(page: Any, say: LogFn) -> bool:
    """Primary Turnstile clicker: target cf-turnstile-response parent checkbox area."""
    try:
        coordinates = await page.evaluate(
            """() => {
                const out = [];
                const pushRect = (el, reason) => {
                    if (!el) return;
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    if (
                        rect.width > 40 &&
                        rect.height > 20 &&
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        Number(style.opacity || '1') > 0.01
                    ) {
                        out.push({ x: rect.x, y: rect.y, w: rect.width, h: rect.height, reason });
                    }
                };

                document
                    .querySelectorAll('[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]')
                    .forEach((el) => {
                        pushRect(
                            el.closest('.cf-turnstile, [data-sitekey], form, div') || el.parentElement,
                            'cf-turnstile-response-parent'
                        );
                    });

                if (!out.length) {
                    document.querySelectorAll('.cf-turnstile, [data-sitekey]').forEach((el) => {
                        pushRect(el, 'cf-turnstile-widget');
                    });
                }

                if (!out.length) {
                    document.querySelectorAll('div').forEach((el) => {
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        const noChildren = !el.querySelector('*');
                        if (
                            noChildren &&
                            rect.width > 290 &&
                            rect.width <= 310 &&
                            rect.height >= 20 &&
                            rect.height <= 120 &&
                            style.display !== 'none' &&
                            style.visibility !== 'hidden'
                        ) {
                            out.push({ x: rect.x, y: rect.y, w: rect.width, h: rect.height, reason: 'empty-300px-div' });
                        }
                    });
                }

                return out.slice(0, 5);
            }"""
        )
    except Exception as exc:
        say(f"[Captcha] Turnstile 坐标探测失败: {exc.__class__.__name__}: {str(exc)[:120]}")
        return False

    if not isinstance(coordinates, list) or not coordinates:
        return False

    for item in coordinates:
        try:
            width = float(item.get("w", 0))
            height = float(item.get("h", 0))
            reason = str(item.get("reason") or "")
            if reason == "cf-turnstile-response-parent" and (width > 360 or height > 160):
                continue
            x = float(item.get("x", 0)) + min(30.0, max(18.0, width * 0.12))
            y = float(item.get("y", 0)) + height / 2
            try:
                await page.mouse.move(x - 18, y - 6, steps=8)
                await page.wait_for_timeout(120)
                await page.mouse.move(x, y, steps=6)
            except Exception:
                pass
            await page.mouse.click(x, y)
            say(f"[Captcha] 已按 Turnstile 坐标点击: {item.get('reason', 'unknown')}")
            return True
        except Exception as exc:
            say(f"[Captcha] Turnstile 坐标点击失败: {exc.__class__.__name__}: {str(exc)[:120]}")
            continue
    return False


CAPTCHA_CLICK_PRIMARY_STRATEGY = "Turnstile坐标主策略(cf-turnstile-response-parent)"
CAPTCHA_MAX_CLICK_ATTEMPTS = 4
CAPTCHA_RETRY_INTERVAL_SEC = 4
CAPTCHA_CLEAR_WAIT_SEC = 8
CAPTCHA_INTERSTITIAL_AUTO_WAIT_SEC = 18
CAPTCHA_INTERSTITIAL_CLEAR_WAIT_SEC = 22


async def is_cloudflare_interstitial(page: Any) -> bool:
    try:
        url = (page.url or "").lower()
    except Exception:
        return False
    if "__cf_chl" in url or "cf_chl" in url:
        return True
    try:
        return bool(
            await page.evaluate(
                """() => {
                    const text = (document.body?.innerText || '').toLowerCase();
                    const hints = [
                        'just a moment',
                        'checking your browser',
                        '正在进行安全验证',
                        'verify you are human',
                    ];
                    if (hints.some((h) => text.includes(h))) return true;
                    return !!document.querySelector(
                        'iframe[src*="challenges.cloudflare.com"], #challenge-running, #cf-challenge-running'
                    );
                }"""
            )
        )
    except Exception:
        return False


def _headed_captcha_fallback_enabled() -> bool:
    env = load_env(".env")
    return str(env.get("CURSOR_HEADLESS_CAPTCHA_FALLBACK") or "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "y",
    }


def captcha_strategy_summary() -> str:
    mode = _captcha_mode()
    parts = [f"模式={mode}", f"主策略={CAPTCHA_CLICK_PRIMARY_STRATEGY}"]
    from .turnstile_solver_client import is_local_solver_configured

    if is_local_solver_configured() and should_use_local_solver():
        parts.append("本地Solver(Camoufox)")
    if should_use_capsolver():
        parts.append("CapSolver(AntiCF+Turnstile)")
    return ", ".join(parts)


def _normalize_capsolver_proxy(raw: str) -> str:
    """CapSolver AntiCloudflareTask 代理格式: host:port:user:pass"""
    value = (raw or "").strip()
    if not value:
        return ""
    if "://" in value:
        from urllib.parse import urlparse

        parsed = urlparse(value)
        if not parsed.hostname or not parsed.port:
            return ""
        user = parsed.username or ""
        pwd = parsed.password or ""
        if user:
            return f"{parsed.hostname}:{parsed.port}:{user}:{pwd}"
        return f"{parsed.hostname}:{parsed.port}"
    return value


def _capsolver_proxy_string() -> str:
    env = load_env(".env")
    explicit = (env.get("CURSOR_CAPTCHA_PROXY") or env.get("CAPSOLVER_PROXY") or "").strip()
    if explicit:
        return _normalize_capsolver_proxy(explicit)
    try:
        from .residential_proxy import build_residential_proxy_password, load_residential_proxy_config

        cfg = load_residential_proxy_config(env)
        if cfg and cfg.enabled:
            pwd = build_residential_proxy_password(cfg, pick_id=1)
            return f"{cfg.server}:{cfg.port}:{cfg.username}:{pwd}"
    except Exception:
        pass
    return ""


def _cookie_domain_for_page(page_url: str) -> str:
    from urllib.parse import urlparse

    host = (urlparse(page_url).hostname or "").strip().lower()
    if not host:
        return ""
    if host.count(".") >= 2:
        return "." + host.split(".", 1)[-1]
    return host


async def _click_challenge_iframes(page: Any, say: LogFn) -> bool:
    for iframe_sel in _TURNSTILE_IFRAME_SELECTORS:
        try:
            fl = page.frame_locator(iframe_sel).first
            for inner in _CLICK_TARGETS:
                try:
                    loc = fl.locator(inner).first
                    if await _click_locator_candidate(loc):
                        say(f"[Captcha] 已点击 iframe 验证区域: {inner}")
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        if await _click_iframe_widget_area(page, iframe_sel, say):
            return True

    for frame in page.frames:
        frame_url = (frame.url or "").lower()
        looks_like_challenge = any(k in frame_url for k in ("cloudflare", "turnstile", "challenge"))
        has_widget = await _frame_has_cloudflare_widget(frame)
        if not looks_like_challenge and not has_widget:
            continue
        if has_widget and await _click_cloudflare_widget_dom(frame, say):
            return True
        if await _click_in_frame(frame, say):
            return True
    return False


async def try_click_security_challenge(
    page: Any,
    say: LogFn,
    *,
    attempt: int = 1,
    max_attempts: int = CAPTCHA_MAX_CLICK_ATTEMPTS,
    log_strategy: bool = True,
) -> bool:
    """在浏览器中点击 Turnstile / Cloudflare 验证组件。"""
    if log_strategy and attempt <= 1:
        say(f"[Captcha] 第 {attempt}/{max_attempts} 次点击: {CAPTCHA_CLICK_PRIMARY_STRATEGY}")
    else:
        say(f"[Captcha] 第 {attempt}/{max_attempts} 次点击验证框...")

    interstitial = await is_cloudflare_interstitial(page)
    if interstitial:
        if await _click_challenge_iframes(page, say):
            return True
        if await _click_turnstile_like_real_browser(page, say):
            return True
    else:
        if await _click_turnstile_like_real_browser(page, say):
            return True
        if await _click_challenge_iframes(page, say):
            return True

    try:
        widget = page.locator(".cf-turnstile, [data-sitekey]").first
        if await widget.count() > 0:
            box = await widget.bounding_box()
            if box:
                await page.mouse.click(
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                )
                say("[Captcha] 已点击 Turnstile 组件中心")
                return True
    except Exception:
        pass

    say("[Captcha] 未找到可点击的验证框")
    return False


async def _capsolver_create_and_wait(task: dict[str, Any], api_key: str, *, log_fn: LogFn) -> str:
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.capsolver.com/createTask",
            json={"clientKey": api_key, "task": task},
        )
        created = resp.json()
    if created.get("errorId"):
        raise RuntimeError(str(created.get("errorDescription") or created))

    task_id = created.get("taskId")
    if not task_id:
        raise RuntimeError(f"CapSolver 未返回 taskId: {created}")

    log_fn(f"[Captcha] CapSolver 任务已创建: {task_id}")

    for _ in range(60):
        await asyncio.sleep(3)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
            )
            result = resp.json()
        status = result.get("status", "")
        if status == "ready":
            solution = result.get("solution") or {}
            token = (
                solution.get("token")
                or solution.get("gRecaptchaResponse")
                or solution.get("cfClearance")
                or ""
            )
            if token:
                return str(token)
            raise RuntimeError(f"CapSolver ready 但无 token: {result}")
        if status == "failed":
            raise RuntimeError(str(result.get("errorDescription") or result))

    raise RuntimeError("CapSolver 等待超时（180s）")


async def solve_turnstile_capsolver(api_key: str, page_url: str, site_key: str, *, log_fn: LogFn = log) -> str:
    task = {
        "type": "AntiTurnstileTaskProxyLess",
        "websiteURL": page_url,
        "websiteKey": site_key,
    }
    log_fn(f"[Captcha] CapSolver 求解 Turnstile: sitekey={site_key[:24]}...")
    return await _capsolver_create_and_wait(task, api_key, log_fn=log_fn)


async def solve_recaptcha_capsolver(
    api_key: str,
    page_url: str,
    site_key: str,
    *,
    version: str = "v2",
    log_fn: LogFn = log,
) -> str:
    if version == "v3":
        task: dict[str, Any] = {
            "type": "ReCaptchaV3TaskProxyLess",
            "websiteURL": page_url,
            "websiteKey": site_key,
            "pageAction": "verify",
            "minScore": 0.3,
        }
    else:
        task = {
            "type": "ReCaptchaV2TaskProxyLess",
            "websiteURL": page_url,
            "websiteKey": site_key,
        }
    log_fn(f"[Captcha] CapSolver 求解 reCAPTCHA {version}: sitekey={site_key[:24]}...")
    return await _capsolver_create_and_wait(task, api_key, log_fn=log_fn)


async def inject_turnstile_token(page: Any, token: str) -> None:
    await page.evaluate(
        """(token) => {
            const setVal = (el) => {
                if (!el) return;
                el.value = token;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            };
            document.querySelectorAll(
                'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
            ).forEach(setVal);
            const widget = document.querySelector('[data-sitekey], .cf-turnstile');
            if (widget) {
                const cb = widget.getAttribute('data-callback');
                if (cb && typeof window[cb] === 'function') {
                    try { window[cb](token); } catch {}
                }
            }
        }""",
        token,
    )


async def inject_recaptcha_token(page: Any, token: str) -> None:
    await page.evaluate(
        """(token) => {
            const ensure = (sel, id, name) => {
                let el = document.querySelector(sel);
                if (!el) {
                    el = document.createElement('textarea');
                    el.id = id;
                    el.name = name;
                    el.style.display = 'none';
                    document.body.appendChild(el);
                }
                el.value = token;
                el.dispatchEvent(new Event('input', { bubbles: true }));
            };
            ensure('#g-recaptcha-response', 'g-recaptcha-response', 'g-recaptcha-response');
            ensure('textarea[name="g-recaptcha-response"]', 'g-recaptcha-response', 'g-recaptcha-response');
            if (window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients) {
                const clients = window.___grecaptcha_cfg.clients;
                for (const id of Object.keys(clients)) {
                    const client = clients[id];
                    for (const key of Object.keys(client || {})) {
                        const item = client[key];
                        if (item && typeof item.callback === 'function') {
                            try { item.callback(token); } catch {}
                        }
                    }
                }
            }
        }""",
        token,
    )


async def inject_cf_cookies(page: Any, cookies: dict[str, str], *, page_url: str = "") -> None:
    if not cookies:
        return
    url = page_url or getattr(page, "url", "") or ""
    domain = _cookie_domain_for_page(url)
    if not domain:
        return
    context = page.context
    payload = []
    for name, value in cookies.items():
        if not name or not value:
            continue
        payload.append(
            {
                "name": str(name),
                "value": str(value),
                "domain": domain,
                "path": "/",
            }
        )
    if payload:
        await context.add_cookies(payload)


async def _maybe_save_cf_storage(page: Any) -> None:
    env = load_env(".env")
    save_path = (env.get("CURSOR_CF_STORAGE_STATE") or "").strip()
    if not save_path or str(env.get("CURSOR_CF_STORAGE_SAVE") or "").lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return
    from pathlib import Path

    from .utils import resolve_path

    path = resolve_path(save_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await page.context.storage_state(path=str(path))
        log(f"[Captcha] 已保存 CF 会话: {path}")
    except Exception as exc:
        log(f"[Captcha] 保存 CF 会话失败: {exc.__class__.__name__}: {exc}")


async def solve_anti_cloudflare_capsolver(
    api_key: str,
    page_url: str,
    proxy: str,
    *,
    html: str = "",
    user_agent: str = "",
    log_fn: LogFn = log,
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "type": "AntiCloudflareTask",
        "websiteURL": page_url,
        "proxy": proxy,
    }
    if html:
        task["html"] = html[:120_000]
    if user_agent:
        task["userAgent"] = user_agent
    log_fn(f"[Captcha] CapSolver AntiCloudflare: {page_url[:80]}...")
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.capsolver.com/createTask",
            json={"clientKey": api_key, "task": task},
        )
        created = resp.json()
    if created.get("errorId"):
        raise RuntimeError(str(created.get("errorDescription") or created))
    task_id = created.get("taskId")
    if not task_id:
        raise RuntimeError(f"CapSolver 未返回 taskId: {created}")
    log_fn(f"[Captcha] CapSolver AntiCloudflare 任务: {task_id}")
    for _ in range(60):
        await asyncio.sleep(3)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
            )
            result = resp.json()
        status = result.get("status", "")
        if status == "ready":
            solution = result.get("solution") or {}
            if solution:
                return solution
            raise RuntimeError(f"CapSolver ready 但无 solution: {result}")
        if status == "failed":
            raise RuntimeError(str(result.get("errorDescription") or result))
    raise RuntimeError("CapSolver AntiCloudflare 等待超时（180s）")


async def _try_local_turnstile_solver(page: Any, say: LogFn) -> bool:
    from .turnstile_solver_client import solve_turnstile_via_local_solver

    turnstile = await extract_turnstile_info(page)
    site_key = (turnstile.get("sitekey") or "").strip()
    page_url = (turnstile.get("pageurl") or page.url or "").strip()
    if not site_key:
        say("[Captcha] 本地 Solver 跳过：页面无 sitekey（CF 全页挑战需 CapSolver AntiCloudflare）")
        return False
    try:
        token = await solve_turnstile_via_local_solver(
            page_url=page_url,
            sitekey=site_key,
            log_fn=say,
        )
        await inject_turnstile_token(page, token)
        await _try_submit_cf_challenge(page)
        await page.wait_for_timeout(2000)
        if await _wait_challenge_clear(page, seconds=20):
            say("[Captcha] 本地 Solver 已通过")
            await _maybe_save_cf_storage(page)
            return True
    except Exception as exc:
        say(f"[Captcha] 本地 Solver 失败: {exc.__class__.__name__}: {exc}")
    return False


async def _try_anti_cloudflare_capsolver(page: Any, say: LogFn) -> bool:
    api_key = _capsolver_key()
    proxy = _capsolver_proxy_string()
    if not api_key:
        return False
    if not proxy:
        say("[Captcha] CapSolver AntiCloudflare 跳过：未配置代理（需 CURSOR_CAPTCHA_PROXY 或 Kookeey）")
        return False
    page_url = page.url or ""
    html = ""
    try:
        html = await page.content()
    except Exception:
        pass
    try:
        solution = await solve_anti_cloudflare_capsolver(
            api_key,
            page_url,
            proxy,
            html=html if "just a moment" in html.lower() else "",
            log_fn=say,
        )
        cookies = dict(solution.get("cookies") or {})
        token = str(solution.get("token") or "")
        if token and "cf_clearance" not in cookies:
            cookies["cf_clearance"] = token
        await inject_cf_cookies(page, cookies, page_url=page_url)
        await page.wait_for_timeout(1500)
        try:
            await page.reload(wait_until="domcontentloaded", timeout=60_000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)
        if await _wait_challenge_clear(page, seconds=25):
            say("[Captcha] CapSolver AntiCloudflare 已通过")
            await _maybe_save_cf_storage(page)
            return True
    except Exception as exc:
        say(f"[Captcha] CapSolver AntiCloudflare 失败: {exc.__class__.__name__}: {exc}")
    return False


async def _try_capsolver(page: Any, say: LogFn) -> bool:
    api_key = _capsolver_key()
    if not api_key:
        return False

    say("[Captcha] 点击未通过，尝试 CapSolver 打码...")

    turnstile = await extract_turnstile_info(page)
    site_key = turnstile.get("sitekey", "")
    page_url = turnstile.get("pageurl") or page.url

    if site_key:
        try:
            token = await solve_turnstile_capsolver(api_key, page_url, site_key, log_fn=say)
            await inject_turnstile_token(page, token)
            await page.wait_for_timeout(3000)
            if await _wait_challenge_clear(page, seconds=15):
                say("[Captcha] CapSolver Turnstile 已通过")
                return True
        except Exception as exc:
            say(f"[Captcha] CapSolver Turnstile 失败: {exc.__class__.__name__}: {exc}")

    recaptcha = await extract_recaptcha_info(page)
    if recaptcha.get("sitekey"):
        try:
            token = await solve_recaptcha_capsolver(
                api_key,
                recaptcha.get("pageurl") or page_url,
                recaptcha["sitekey"],
                version=str(recaptcha.get("version") or "v2"),
                log_fn=say,
            )
            await inject_recaptcha_token(page, token)
            await page.wait_for_timeout(3000)
            if await _wait_challenge_clear(page, seconds=15):
                say("[Captcha] CapSolver reCAPTCHA 已通过")
                return True
        except Exception as exc:
            say(f"[Captcha] CapSolver reCAPTCHA 失败: {exc.__class__.__name__}: {exc}")

    return False


async def try_auto_clear_security_challenge(
    page: Any,
    say: LogFn,
    *,
    try_capsolver: bool = True,
    attempt: int = 1,
    max_attempts: int = CAPTCHA_MAX_CLICK_ATTEMPTS,
    log_strategy: bool = True,
) -> bool:
    """过安全验证：先点击，再可选 CapSolver。成功返回 True。"""
    mode = _captcha_mode()
    if mode == "manual":
        return False

    interstitial = await is_cloudflare_interstitial(page)
    clear_wait_sec = CAPTCHA_INTERSTITIAL_CLEAR_WAIT_SEC if interstitial else CAPTCHA_CLEAR_WAIT_SEC

    if log_strategy and attempt <= 1:
        say(f"[Captcha] 自动验证策略: {captcha_strategy_summary()}")
    elif attempt > 1:
        say(f"[Captcha] 第 {attempt}/{max_attempts} 次自动验证")

    if attempt == 1 and interstitial:
        say(f"[Captcha] CF 全页挑战，先等待自动通过（{CAPTCHA_INTERSTITIAL_AUTO_WAIT_SEC}s）...")
        if await _wait_challenge_clear(page, seconds=CAPTCHA_INTERSTITIAL_AUTO_WAIT_SEC):
            say("[Captcha] CF 全页挑战已自动通过")
            await _maybe_save_cf_storage(page)
            return True

    if should_use_local_solver():
        if await _try_local_turnstile_solver(page, say):
            return True

    if try_capsolver and should_use_capsolver() and interstitial:
        if await _try_anti_cloudflare_capsolver(page, say):
            return True

    if should_try_click() and not interstitial:
        if await try_click_security_challenge(
            page,
            say,
            attempt=attempt,
            max_attempts=max_attempts,
            log_strategy=False,
        ):
            if await _wait_challenge_clear(page, seconds=clear_wait_sec):
                say("[Captcha] 点击验证框后已通过")
                return True
            if await turnstile_token_ready(page):
                await _try_submit_cf_challenge(page)
                if await _wait_challenge_clear(page, seconds=12):
                    say("[Captcha] Turnstile token 提交后已通过")
                    return True
            say("[Captcha] 点击候选控件后验证仍未通过")
        elif await _wait_challenge_clear(page, seconds=2):
            visible, _ = await security_challenge_visible(page)
            if not visible:
                say("[Captcha] 安全验证已自动通过（无需点击）")
                return True

    if should_try_click() and interstitial and attempt >= 3:
        if await try_click_security_challenge(
            page,
            say,
            attempt=attempt,
            max_attempts=max_attempts,
            log_strategy=False,
        ):
            if await _wait_challenge_clear(page, seconds=clear_wait_sec):
                say("[Captcha] 点击验证框后已通过")
                return True

    if try_capsolver and should_use_capsolver():
        if await _try_capsolver(page, say):
            return True

    return False
