from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .utils import log, resolve_path


def parse_proxy(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    raw = value.strip()
    raw = raw.lstrip("\ufeff\u200b\u2060")
    if not raw:
        return None
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    if not parsed.hostname or not parsed.port:
        raise ValueError("代理格式错误，应为 host:port、http://host:port 或 socks5://host:port")
    proxy = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


SYSTEM_CHROME_PATHS = [
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
]


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _resolve_system_chrome_executable() -> str:
    override = os.environ.get("PLAYWRIGHT_CHROME_EXECUTABLE", "").strip()
    if override and Path(override).exists():
        return override
    for path in SYSTEM_CHROME_PATHS:
        if path.exists():
            return str(path)
    return ""


def _bundled_playwright_browsers_exist() -> bool:
    internal_dir = Path(__file__).resolve().parents[1]
    local_browsers_dir = internal_dir / "playwright" / "driver" / "package" / ".local-browsers"
    return local_browsers_dir.exists()


def _should_use_system_chrome(incognito: bool = False) -> bool:
    if _env_flag("PLAYWRIGHT_USE_SYSTEM_CHROME", default=False):
        return True
    if incognito and _resolve_system_chrome_executable():
        return True
    return bool(getattr(sys, "frozen", False)) and not _bundled_playwright_browsers_exist()


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _launch_args(*, incognito: bool) -> list[str]:
    base = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if incognito:
        return ["--incognito", *base]
    return base


def _chrome_proxy_server_arg(proxy: str | None) -> str | None:
    proxy_cfg = parse_proxy(proxy)
    if not proxy_cfg:
        return None
    # Chrome 命令行代理不支持 Playwright 形式的 username/password，带鉴权代理走 Playwright 回退。
    if proxy_cfg.get("username") or proxy_cfg.get("password"):
        return None
    return f'--proxy-server={proxy_cfg["server"]}'


def _chrome_proxy_cli_args(proxy: str | None) -> list[str]:
    """未配置代理时显式禁用系统代理，避免 Clash 关闭后仍走 127.0.0.1:7897。"""
    proxy_arg = _chrome_proxy_server_arg(proxy)
    if proxy_arg:
        return [proxy_arg]
    if not (proxy or "").strip():
        return ["--no-proxy-server"]
    return []


def _cf_storage_state_path() -> Path | None:
    raw = os.environ.get("CURSOR_CF_STORAGE_STATE", "").strip()
    if not raw:
        return None
    path = resolve_path(raw)
    return path if path.exists() else None


async def _apply_storage_state(context: BrowserContext, path: Path) -> None:
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        cookies = data.get("cookies") or []
        if cookies:
            await context.add_cookies(cookies)
            log(f"[Browser] 已加载 CF 会话 Cookie ({len(cookies)} 条): {path}")
    except Exception as exc:
        log(f"[Browser] 加载 CF 会话失败: {exc.__class__.__name__}: {exc}")


class BrowserSession:
    def __init__(
        self,
        profile_dir: str | Path,
        headless: bool,
        slow_mo: int,
        timeout_ms: int,
        proxy: str | None = None,
        incognito: bool = False,
        **kwargs,
    ):
        self.profile_dir = resolve_path(profile_dir)
        self.headless = headless
        self.slow_mo = slow_mo
        self.timeout_ms = timeout_ms
        self.proxy = proxy
        self.incognito = incognito
        self._playwright = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._chrome_proc: subprocess.Popen | None = None
        self._cdp_connected = False
        self._temp_user_data_dir: str | None = None

    async def _launch_incognito_via_cdp(self) -> bool:
        chrome = _resolve_system_chrome_executable()
        if not chrome:
            return False

        port = _pick_free_port()
        self._temp_user_data_dir = tempfile.mkdtemp(prefix="cursor_chrome_cdp_")
        args = [
            chrome,
            f"--user-data-dir={self._temp_user_data_dir}",
            "--incognito",
            f"--remote-debugging-port={port}",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if self.headless:
            args.append("--headless=new")
        args.extend(_chrome_proxy_cli_args(self.proxy))

        self._chrome_proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        endpoint = f"http://127.0.0.1:{port}"
        last_error: Exception | None = None
        for _ in range(80):
            if self._chrome_proc.poll() is not None:
                stderr = ""
                try:
                    stderr = (self._chrome_proc.stderr.read() or b"").decode("utf-8", "ignore")[:240]
                except Exception:
                    pass
                last_error = RuntimeError(f"Chrome 无痕进程已退出{(': ' + stderr) if stderr else ''}")
                break
            try:
                self.browser = await self._playwright.chromium.connect_over_cdp(endpoint)
                if self.browser.contexts:
                    self._cdp_connected = True
                    self.context = self.browser.contexts[0]
                    log(f"[Browser] 已启动 Chrome 无痕窗口 (CDP:{port})")
                    return True
            except Exception as exc:
                last_error = exc
            await asyncio.sleep(0.25)

        self._terminate_chrome_process()
        log(f"[Browser] CDP 无痕启动失败，将回退 Playwright: {last_error}")
        return False

    async def _launch_incognito_playwright(self) -> None:
        channel = "chrome" if _should_use_system_chrome(True) and _resolve_system_chrome_executable() else None
        self._temp_user_data_dir = tempfile.mkdtemp(prefix="cursor_incognito_")
        launch_kwargs: dict = {
            "user_data_dir": self._temp_user_data_dir,
            "headless": self.headless,
            "slow_mo": self.slow_mo,
            "proxy": parse_proxy(self.proxy),
            "args": [*_launch_args(incognito=True), *_chrome_proxy_cli_args(self.proxy)],
            "viewport": {"width": 1365, "height": 900},
        }
        storage_path = _cf_storage_state_path()
        if storage_path:
            launch_kwargs["storage_state"] = str(storage_path)
        if channel:
            launch_kwargs["channel"] = channel
        log(f"[Browser] 无痕回退: 临时 Chrome profile (channel={channel or 'chromium'})")
        self.context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)

    def _terminate_chrome_process(self) -> None:
        proc = self._chrome_proc
        self._chrome_proc = None
        if not proc:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _cleanup_temp_dirs(self) -> None:
        temp_dir = self._temp_user_data_dir
        self._temp_user_data_dir = None
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def __aenter__(self) -> "BrowserSession":
        if not self.incognito:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()

        if self.incognito:
            use_cdp = _chrome_proxy_server_arg(self.proxy) is not None or not self.proxy
            cdp_ok = use_cdp and await self._launch_incognito_via_cdp()
            if not cdp_ok:
                if self.proxy and not use_cdp:
                    log("[Browser] 代理含账号密码，使用 Playwright 临时 profile（支持代理鉴权）")
                await self._launch_incognito_playwright()
        else:
            launch_kwargs: dict = {
                "headless": self.headless,
                "slow_mo": self.slow_mo,
                "args": [*_launch_args(incognito=False), *_chrome_proxy_cli_args(self.proxy)],
                "proxy": parse_proxy(self.proxy),
                "user_data_dir": str(self.profile_dir),
                "viewport": {"width": 1365, "height": 900},
            }
            storage_path = _cf_storage_state_path()
            if storage_path:
                launch_kwargs["storage_state"] = str(storage_path)
            if _should_use_system_chrome(False) and _resolve_system_chrome_executable():
                launch_kwargs["channel"] = "chrome"
            self.context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)

        assert self.context is not None
        if self.incognito and self._cdp_connected:
            storage_path = _cf_storage_state_path()
            if storage_path:
                await _apply_storage_state(self.context, storage_path)
        self.context.set_default_timeout(self.timeout_ms)
        pages = [page for page in self.context.pages if not page.is_closed()]
        self.page = pages[0] if pages else await self.context.new_page()
        return self

    async def current_page(self) -> Page:
        if not self.context:
            raise RuntimeError("浏览器上下文未启动")
        if self.page and not self.page.is_closed():
            return self.page
        pages = [page for page in self.context.pages if not page.is_closed()]
        if pages:
            self.page = pages[-1]
            return self.page
        self.page = await self.context.new_page()
        return self.page

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._cdp_connected:
            try:
                if self.browser:
                    await self.browser.close()
            except Exception:
                pass
            self._terminate_chrome_process()
        else:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._cleanup_temp_dirs()
