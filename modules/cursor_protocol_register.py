from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .cursor_browser_flow import CURSOR_SIGNUP_URL
from .utils import env_bool, load_env, log


CURSOR_WORKOS_CLIENT_ID = "client_01GS6W3C96KW4WRS6Z93JCE2RJ"
CURSOR_AUTH_LOGIN_URL = "https://cursor.com/api/auth/login"
CURSOR_AUTH_CALLBACK = "https://cursor.com/api/auth/callback"
CURSOR_AUTHENTICATOR_HOST = "authenticator.cursor.sh"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_ACTION_HASH_RE = re.compile(r'(?:Next-Action|next-action)["\s:=]+([a-f0-9]{32,64})', re.I)
_ACTION_ID_RE = re.compile(r"\$ACTION_ID_([a-zA-Z0-9_]+)")
_AUTH_SESSION_RE = re.compile(r"authorization_session_id=([A-Z0-9]+)")


@dataclass
class CursorAuthContext:
    client_id: str
    redirect_uri: str
    state: str
    authorization_session_id: str
    final_url: str
    bootstrap_url: str
    cookies: dict[str, str] = field(default_factory=dict)
    cf_blocked: bool = False
    http_status: int = 0


@dataclass
class CursorProtocolProbe:
    direct_signup_status: int
    direct_signup_cf: bool
    auth_context: CursorAuthContext | None
    authenticated_fetch_ok: bool
    authenticated_status: int
    action_ids: list[str] = field(default_factory=list)


@dataclass
class CursorProtocolSession:
    """HTTP session seeded from Playwright cookies after Cloudflare clears."""

    page_url: str
    cookies: dict[str, str]
    auth_context: CursorAuthContext | None = None
    action_ids: list[str] = field(default_factory=list)
    _client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> CursorProtocolSession:
        self._client = httpx.AsyncClient(
            headers=BROWSER_HEADERS,
            cookies=self.cookies,
            timeout=30,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def fetch(self, url: str) -> tuple[int, str, bool]:
        assert self._client is not None
        resp = await self._client.get(url)
        cf = (resp.headers.get("cf-mitigated") or "").lower() == "challenge"
        return resp.status_code, resp.text, cf

    async def post_next_action(self, action_id: str, payload: list[Any]) -> tuple[int, str]:
        assert self._client is not None
        headers = {
            **BROWSER_HEADERS,
            "Next-Action": action_id,
            "Content-Type": "text/plain;charset=UTF-8",
            "Accept": "text/x-component",
            "Origin": f"https://{CURSOR_AUTHENTICATOR_HOST}",
            "Referer": self.page_url,
        }
        resp = await self._client.post(
            self.page_url,
            content=json.dumps(payload, ensure_ascii=False),
            headers=headers,
        )
        return resp.status_code, resp.text[:500]


def extract_action_ids(html: str) -> list[str]:
    found: list[str] = []
    for pattern in (_ACTION_HASH_RE, _ACTION_ID_RE):
        for match in pattern.finditer(html or ""):
            value = match.group(1)
            if value and value not in found:
                found.append(value)
    return found


def _parse_auth_session_id(url: str, body: str = "") -> str:
    query = parse_qs(urlparse(url).query)
    session = (query.get("authorization_session_id") or [""])[0]
    if session:
        return session
    match = _AUTH_SESSION_RE.search(url) or _AUTH_SESSION_RE.search(body or "")
    return match.group(1) if match else ""


def _signup_url_with_context(ctx: CursorAuthContext) -> str:
    query = {
        "client_id": ctx.client_id,
        "redirect_uri": ctx.redirect_uri,
        "state": ctx.state,
    }
    if ctx.authorization_session_id:
        query["authorization_session_id"] = ctx.authorization_session_id
    return f"{CURSOR_SIGNUP_URL}?{urlencode(query)}"


async def resolve_auth_context(*, proxy: str | None = None) -> CursorAuthContext:
    """Follow cursor.com → WorkOS → bootstrap without a browser (stops at CF)."""
    proxy_url = proxy if proxy and "://" in proxy else (f"http://{proxy}" if proxy else None)
    cookies: dict[str, str] = {}
    client_id = CURSOR_WORKOS_CLIENT_ID
    redirect_uri = CURSOR_AUTH_CALLBACK
    state = ""
    bootstrap_url = ""
    final_url = ""
    http_status = 0
    cf_blocked = False

    async with httpx.AsyncClient(
        headers=BROWSER_HEADERS,
        timeout=30,
        follow_redirects=False,
        proxy=proxy_url,
    ) as client:
        resp = await client.get(CURSOR_AUTH_LOGIN_URL)
        http_status = resp.status_code
        url = resp.headers.get("location") or ""
        for hop in range(8):
            if not url:
                break
            parsed = urlparse(url)
            q = parse_qs(parsed.query)
            client_id = (q.get("client_id") or [client_id])[0] or client_id
            redirect_uri = (q.get("redirect_uri") or [redirect_uri])[0] or redirect_uri
            state = (q.get("state") or [state])[0] or state
            if "bootstrap" in url:
                bootstrap_url = url
            resp = await client.get(url)
            http_status = resp.status_code
            for cookie in resp.cookies.jar:
                cookies[cookie.name] = cookie.value
            final_url = str(resp.url) if hasattr(resp, "url") else url
            cf_blocked = (resp.headers.get("cf-mitigated") or "").lower() == "challenge"
            if cf_blocked:
                break
            url = resp.headers.get("location") or ""
            if resp.status_code not in {301, 302, 303, 307, 308}:
                final_url = url or final_url
                break

    session_id = _parse_auth_session_id(final_url or bootstrap_url, "")
    return CursorAuthContext(
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        authorization_session_id=session_id,
        final_url=final_url or bootstrap_url,
        bootstrap_url=bootstrap_url,
        cookies=dict(cookies),
        cf_blocked=cf_blocked,
        http_status=http_status,
    )


async def probe_pure_protocol(*, proxy: str | None = None) -> CursorProtocolProbe:
    proxy_url = proxy if proxy and "://" in proxy else (f"http://{proxy}" if proxy else None)
    async with httpx.AsyncClient(
        headers=BROWSER_HEADERS,
        timeout=30,
        follow_redirects=False,
        proxy=proxy_url,
    ) as client:
        direct = await client.get(CURSOR_SIGNUP_URL)
    auth_ctx = await resolve_auth_context(proxy=proxy)
    return CursorProtocolProbe(
        direct_signup_status=direct.status_code,
        direct_signup_cf=(direct.headers.get("cf-mitigated") or "").lower() == "challenge",
        auth_context=auth_ctx,
        authenticated_fetch_ok=False,
        authenticated_status=0,
    )


async def session_from_playwright(page: Any, *, auth_context: CursorAuthContext | None = None) -> CursorProtocolSession:
    context = page.context
    cookies = {
        c["name"]: c["value"]
        for c in await context.cookies()
        if "cursor" in (c.get("domain") or "")
    }
    page_url = page.url or CURSOR_SIGNUP_URL
    session_id = _parse_auth_session_id(page_url)
    ctx = auth_context
    if ctx is None:
        ctx = CursorAuthContext(
            client_id=CURSOR_WORKOS_CLIENT_ID,
            redirect_uri=CURSOR_AUTH_CALLBACK,
            state="",
            authorization_session_id=session_id,
            final_url=page_url,
            bootstrap_url="",
            cookies=cookies,
        )
    elif session_id and not ctx.authorization_session_id:
        ctx = CursorAuthContext(
            client_id=ctx.client_id,
            redirect_uri=ctx.redirect_uri,
            state=ctx.state,
            authorization_session_id=session_id,
            final_url=page_url,
            bootstrap_url=ctx.bootstrap_url,
            cookies={**ctx.cookies, **cookies},
            cf_blocked=ctx.cf_blocked,
            http_status=ctx.http_status,
        )
    else:
        ctx = CursorAuthContext(
            client_id=ctx.client_id,
            redirect_uri=ctx.redirect_uri,
            state=ctx.state,
            authorization_session_id=ctx.authorization_session_id or session_id,
            final_url=page_url,
            bootstrap_url=ctx.bootstrap_url,
            cookies={**ctx.cookies, **cookies},
            cf_blocked=ctx.cf_blocked,
            http_status=ctx.http_status,
        )
    return CursorProtocolSession(page_url=page_url, cookies=cookies, auth_context=ctx)


async def probe_with_browser_cookies(session: CursorProtocolSession) -> CursorProtocolProbe:
    direct_signup_status = 403
    direct_signup_cf = True
    auth_fetch_ok = False
    auth_status = 0
    action_ids: list[str] = []

    async with session:
        status, _body, cf = await session.fetch(CURSOR_SIGNUP_URL)
        direct_signup_status = status
        direct_signup_cf = cf

        target = session.page_url
        if session.auth_context and session.auth_context.authorization_session_id:
            if "sign-up" not in (target or ""):
                target = _signup_url_with_context(session.auth_context)
        status, body, cf = await session.fetch(target)
        auth_status = status
        auth_fetch_ok = status == 200 and not cf
        if body:
            action_ids = extract_action_ids(body)

    return CursorProtocolProbe(
        direct_signup_status=direct_signup_status,
        direct_signup_cf=direct_signup_cf,
        auth_context=session.auth_context,
        authenticated_fetch_ok=auth_fetch_ok,
        authenticated_status=auth_status,
        action_ids=action_ids,
    )


def format_probe_summary(probe: CursorProtocolProbe) -> str:
    parts = [
        f"直连 sign-up HTTP {probe.direct_signup_status}"
        + (" (Cloudflare)" if probe.direct_signup_cf else ""),
    ]
    ctx = probe.auth_context
    if ctx:
        parts.append(
            f"授权链 session={ctx.authorization_session_id or 'n/a'} "
            f"bootstrap={'yes' if ctx.bootstrap_url else 'no'} "
            f"cookies={len(ctx.cookies)} cf_blocked={ctx.cf_blocked}"
        )
    if probe.authenticated_status:
        parts.append(
            f"带 Cookie 拉页 HTTP {probe.authenticated_status}"
            + (" OK" if probe.authenticated_fetch_ok else " (仍被 CF/失败)")
        )
    if probe.action_ids:
        parts.append(f"发现 Next-Action {len(probe.action_ids)} 个")
    return "; ".join(parts)


async def log_protocol_probe(prefix: str, probe: CursorProtocolProbe) -> None:
    log(f"{prefix} [Protocol] {format_probe_summary(probe)}")


async def try_protocol_submit_signup(
    proto: CursorProtocolSession,
    *,
    email: str,
    first_name: str,
    last_name: str,
    password: str,
    prefix: str,
) -> bool:
    if not proto.action_ids:
        async with proto:
            _, body, cf = await proto.fetch(proto.page_url)
            if cf or not body:
                return False
            proto.action_ids = extract_action_ids(body)
    if not proto.action_ids:
        log(f"{prefix} [Protocol] 页面上未解析到 Next-Action，跳过 HTTP 提交")
        return False

    payload = [
        {
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "password": password,
        }
    ]
    async with proto:
        for action_id in proto.action_ids[:3]:
            status, snippet = await proto.post_next_action(action_id, payload)
            log(f"{prefix} [Protocol] POST Next-Action {action_id[:16]}… → HTTP {status}")
            if status in {200, 303, 307, 308} and "error" not in snippet.lower():
                return True
    return False


def protocol_enabled_for_env() -> bool:
    env = load_env(".env")
    return env_bool(env.get("CURSOR_REGISTER_PROTOCOL"), default=False)


def protocol_fallback_browser() -> bool:
    env = load_env(".env")
    return env_bool(env.get("CURSOR_PROTOCOL_FALLBACK_BROWSER"), default=True)


async def bootstrap_from_browser(
    page: Any,
    prefix: str,
) -> tuple[CursorProtocolSession, CursorProtocolProbe]:
    """浏览器过 CF 后仅从 Cookie 引导协议会话，跳过纯 HTTP 探测。"""
    proto = await session_from_playwright(page)
    try:
        browser_probe = await probe_with_browser_cookies(proto)
    except Exception as exc:
        log(f"{prefix} [Protocol] Cookie 探测失败，回退 DOM: {exc.__class__.__name__}")
        browser_probe = CursorProtocolProbe(
            direct_signup_status=0,
            direct_signup_cf=True,
            auth_context=proto.auth_context,
            authenticated_fetch_ok=False,
            authenticated_status=0,
            action_ids=[],
        )
    await log_protocol_probe(prefix, browser_probe)
    proto.action_ids = browser_probe.action_ids
    return proto, browser_probe


async def bootstrap_and_probe(page: Any, prefix: str, *, proxy: str | None = None) -> tuple[CursorProtocolSession, CursorProtocolProbe]:
    pure = await probe_pure_protocol(proxy=proxy)
    await log_protocol_probe(prefix, pure)
    proto = await session_from_playwright(page, auth_context=pure.auth_context)
    browser_probe = await probe_with_browser_cookies(proto)
    await log_protocol_probe(prefix, browser_probe)
    proto.action_ids = browser_probe.action_ids
    return proto, browser_probe


async def run_cursor_protocol_register_once(
    cfg: dict[str, Any],
    *,
    sms_selection: dict[str, object] | None,
    worker_id: int = 1,
    proxy: str | None = None,
    mail_source: Any = None,
) -> bool:
    from . import cursor_register

    return await cursor_register.run_cursor_register_once(
        cfg,
        sms_selection=sms_selection,
        worker_id=worker_id,
        proxy=proxy,
        mail_source=mail_source,
        register_mode="protocol",
    )


async def run_cursor_protocol_register_many(
    cfg: dict[str, Any],
    *,
    count: int,
    workers: int,
    sms_selection: dict[str, object] | None,
) -> int:
    from . import cursor_register

    return await cursor_register.run_cursor_register_many(
        cfg,
        count=count,
        workers=workers,
        sms_selection=sms_selection,
        register_mode="protocol",
    )


def interactive_cursor_protocol_register(
    config_path: str,
    cfg: dict[str, Any],
    sms_selection: dict[str, object] | None,
    ask_positive_int,
) -> int:
    import asyncio

    print()
    print("Cursor 协议注册")
    print("  - 浏览器过 Cloudflare 后优先 httpx/Next-Action 提交")
    print("  - 邮箱验证码 + HeroSMS 手机接码（失败步骤仍走 DOM）")
    print()
    count = ask_positive_int("请输入这次要成功注册多少个", default=1)
    workers = ask_positive_int("请输入并发线程数", default=1, max_value=count)
    return asyncio.run(
        run_cursor_protocol_register_many(cfg, count=count, workers=workers, sms_selection=sms_selection)
    )
