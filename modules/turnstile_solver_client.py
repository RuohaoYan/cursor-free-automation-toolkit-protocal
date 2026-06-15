"""本地 Turnstile Solver 客户端（兼容 Theyka / any-auto-register API）。"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import quote, urlencode

LogFn = Any  # Callable[[str], None] — avoid circular import


class TurnstileSolverError(RuntimeError):
    pass


def _solver_base_url() -> str:
    from .utils import load_env

    env = load_env(".env")
    raw = (
        env.get("TURNSTILE_SOLVER_URL")
        or env.get("CURSOR_TURNSTILE_SOLVER_URL")
        or ""
    ).strip().rstrip("/")
    return raw


def is_local_solver_configured() -> bool:
    return bool(_solver_base_url())


async def check_local_solver_health(*, timeout_sec: float = 3.0) -> bool:
    base = _solver_base_url()
    if not base:
        return False
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            resp = await client.get(f"{base}/")
            return resp.status_code < 500
    except Exception:
        return False


async def solve_turnstile_via_local_solver(
    *,
    page_url: str,
    sitekey: str,
    action: str = "",
    cdata: str = "",
    poll_interval_sec: float = 2.0,
    timeout_sec: float = 120.0,
    log_fn: LogFn | None = None,
) -> str:
    """调用 GET /turnstile + GET /result，返回 Turnstile token。"""
    base = _solver_base_url()
    if not base:
        raise TurnstileSolverError("TURNSTILE_SOLVER_URL 未配置")
    if not page_url or not sitekey:
        raise TurnstileSolverError("page_url 与 sitekey 不能为空")

    params: dict[str, str] = {"url": page_url, "sitekey": sitekey}
    if action:
        params["action"] = action
    if cdata:
        params["cdata"] = cdata

    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        create_url = f"{base}/turnstile?{urlencode(params, quote_via=quote)}"
        if log_fn:
            log_fn(f"[Captcha] 本地 Solver 创建任务: {page_url[:80]} sitekey={sitekey[:20]}...")
        resp = await client.get(create_url)
        data = resp.json()
        task_id = data.get("task_id") or data.get("taskId")
        if not task_id:
            raise TurnstileSolverError(f"本地 Solver 未返回 task_id: {data}")

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval_sec)
            result_resp = await client.get(f"{base}/result", params={"id": task_id})
            result = result_resp.json()
            if result.get("errorId"):
                raise TurnstileSolverError(str(result.get("errorDescription") or result))
            value = result.get("value") or result.get("token")
            if value:
                if log_fn:
                    elapsed = result.get("elapsed_time")
                    log_fn(
                        f"[Captcha] 本地 Solver 成功"
                        + (f" ({elapsed}s)" if elapsed is not None else "")
                    )
                return str(value)
            status = str(result.get("status") or "").lower()
            if status in {"failed", "error"}:
                raise TurnstileSolverError(str(result.get("errorDescription") or result))
        raise TurnstileSolverError(f"本地 Solver 超时 ({int(timeout_sec)}s)")
