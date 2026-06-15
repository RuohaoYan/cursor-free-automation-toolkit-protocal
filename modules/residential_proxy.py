from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .utils import load_env


@dataclass(frozen=True)
class ResidentialProxyConfig:
    enabled: bool
    server: str
    port: int
    proxy_type: str
    username: str
    password: str
    region: str = ""
    session_duration: str = "1m"
    session_mode: str = "attempt"
    fixed_session: str = ""
    upstream: str = ""


def _is_true(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y", "是", "启用"}


def _parse_int(value: str | None, default: int) -> int:
    try:
        return int(str(value or "").strip())
    except ValueError:
        return default


def _resolve_upstream(values: dict[str, str]) -> str:
    upstream = (
        values.get("RESIDENTIAL_PROXY_UPSTREAM")
        or values.get("PROXY_UPSTREAM")
        or ""
    ).strip()
    if upstream:
        return upstream
    if not _is_true(values.get("USE_PROXY")):
        return ""
    proxy_file = Path(values.get("PROXY_FILE") or "data/proxies/proxies.txt")
    if not proxy_file.is_absolute():
        proxy_file = Path(__file__).resolve().parents[1] / proxy_file
    if not proxy_file.exists():
        return ""
    for raw_line in proxy_file.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw_line.strip().lstrip("\ufeff\u200b\u2060")
        if line and not line.startswith("#"):
            return line
    return ""


def load_residential_proxy_config(env: dict[str, str] | None = None) -> ResidentialProxyConfig | None:
    values = env if env is not None else load_env(".env")
    if not _is_true(values.get("RESIDENTIAL_PROXY_ENABLED")):
        return None
    server = (values.get("RESIDENTIAL_PROXY_SERVER") or "").strip()
    username = (values.get("RESIDENTIAL_PROXY_USERNAME") or "").strip()
    password = (values.get("RESIDENTIAL_PROXY_PASSWORD") or "").strip()
    if not server or not username or not password:
        raise RuntimeError(
            "RESIDENTIAL_PROXY_ENABLED=true 时须配置 RESIDENTIAL_PROXY_SERVER / "
            "RESIDENTIAL_PROXY_USERNAME / RESIDENTIAL_PROXY_PASSWORD"
        )
    proxy_type = (values.get("RESIDENTIAL_PROXY_TYPE") or "http").strip().lower()
    if proxy_type not in {"http", "socks5"}:
        raise RuntimeError("RESIDENTIAL_PROXY_TYPE 仅支持 http 或 socks5")
    return ResidentialProxyConfig(
        enabled=True,
        server=server,
        port=_parse_int(values.get("RESIDENTIAL_PROXY_PORT"), 1000),
        proxy_type=proxy_type,
        username=username,
        password=password,
        region=(values.get("RESIDENTIAL_PROXY_REGION") or "").strip(),
        session_duration=(values.get("RESIDENTIAL_PROXY_SESSION_DURATION") or "1m").strip() or "1m",
        session_mode=(values.get("RESIDENTIAL_PROXY_SESSION_MODE") or "attempt").strip().lower() or "attempt",
        fixed_session=(values.get("RESIDENTIAL_PROXY_SESSION") or "").strip(),
        upstream=_resolve_upstream(values),
    )


def _session_token(config: ResidentialProxyConfig, *, worker_id: int, pick_id: int) -> str:
    mode = config.session_mode
    if mode == "fixed":
        token = config.fixed_session or "fixed"
    elif mode == "worker":
        token = f"w{max(1, worker_id)}"
    elif mode == "random":
        token = f"{random.randint(10_000_000, 99_999_999)}"
    else:
        token = str(max(1, pick_id))
    return re.sub(r"[^0-9A-Za-z_-]", "", token) or "1"


def build_residential_proxy_password(
    config: ResidentialProxyConfig,
    *,
    worker_id: int = 1,
    pick_id: int = 1,
) -> str:
    password = config.password
    if "{session}" in password:
        return password.replace("{session}", _session_token(config, worker_id=worker_id, pick_id=pick_id))
    session = _session_token(config, worker_id=worker_id, pick_id=pick_id)
    if config.region:
        return f"{password}-{config.region}-{session}-{config.session_duration}"
    return password


def build_residential_proxy_url(
    config: ResidentialProxyConfig,
    *,
    worker_id: int = 1,
    pick_id: int = 1,
) -> str:
    password = build_residential_proxy_password(config, worker_id=worker_id, pick_id=pick_id)
    if config.upstream:
        from .proxy_chain_forwarder import ChainGateway, get_chained_local_proxy_url

        cache_key = f"{config.username}:{password}@{config.server}:{config.port}|{config.upstream}"
        return get_chained_local_proxy_url(
            config.upstream,
            ChainGateway(
                host=config.server,
                port=config.port,
                username=config.username,
                password=password,
            ),
            cache_key=cache_key,
        )
    user = quote(config.username, safe="")
    pwd = quote(password, safe="")
    return f"{config.proxy_type}://{user}:{pwd}@{config.server}:{config.port}"
