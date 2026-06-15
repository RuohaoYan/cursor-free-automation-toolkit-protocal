from __future__ import annotations

import asyncio
import json
import shutil
import string
import secrets
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import get_oauth_rt  # type: ignore

from .browser import BrowserSession
from .free_browser_flow import FreeBrowserFlow
from .mail_provider import MailProvider
from .moemail_factory import create_moemail_accounts, split_domains
from .proxy_pool import ProxyPool
from .storage import AccountStore, MailAccount, parse_mail_line
from .utils import load_env, log, now_utc, resolve_path, safe_filename


FREE_OUTPUT_ROOT = "output/free注册"
FREE_ACCOUNT_FILE = "account.txt"
FREE_CPA_DIR = "cpa号池"

# 保护 account.txt 读改写，防止并发 worker 互相覆盖
_ACCOUNT_WRITE_LOCK = asyncio.Lock()
_FREE_POOL_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class FreeOutputPaths:
    root: Path
    account_file: Path
    cpa_dir: Path


@dataclass(frozen=True)
class FreeProfile:
    full_name: str
    age: str
    password: str
    birth_date: str = ""


@dataclass(frozen=True)
class FreeMailSource:
    source_key: str
    account: MailAccount
    mail_cfg: dict[str, Any]
    store: AccountStore | None = None

    @property
    def source(self) -> str:
        return self.source_key


class FreeRegisterError(RuntimeError):
    pass


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return text not in {"0", "false", "no", "off", "关闭", "否"}


def _flow_output_root(flow: str) -> Path:
    if flow.upper() == "CURSOR":
        return resolve_path("output/cursor注册")
    return free_output_paths().root


def resolve_free_mail_config(cfg: dict[str, Any], source_key: str) -> dict[str, Any]:
    base = dict(cfg.get("mail", {}) or {})
    sources = cfg.get("mail_sources") or {}
    source_cfg = dict(sources.get(source_key) or {})
    merged = {**base, **source_cfg}
    merged_source = str(
        merged.get("source")
        or source_cfg.get("source")
        or base.get("source")
        or source_key
    ).strip()
    merged["source"] = merged_source or source_key
    return merged


def _build_free_pool_store(source_key: str, mail_cfg: dict[str, Any], *, flow: str = "FREE") -> AccountStore:
    accounts_file = str(mail_cfg.get("accounts_file") or "").strip()
    raw_pool_file = str(mail_cfg.get("raw_pool_file") or "").strip()
    if not accounts_file:
        raise FreeRegisterError(f"{source_key} 邮箱池缺少 accounts_file")
    if not raw_pool_file:
        raise FreeRegisterError(f"{source_key} 邮箱池缺少 raw_pool_file")
    output_root = _flow_output_root(flow)
    suffix = source_key.replace("_", "-")
    prefix = "cursor" if flow.upper() == "CURSOR" else "free"
    return AccountStore(
        accounts_file=accounts_file,
        raw_pool_file=raw_pool_file,
        success_file=str(output_root / f"{prefix}_pool_{suffix}_success.txt"),
        failed_file=str(output_root / f"{prefix}_pool_{suffix}_failed.txt"),
        in_progress_file=str(output_root / f"{prefix}_pool_{suffix}_in_progress.txt"),
    )


def reset_free_pool_claims_on_start(
    cfg: dict[str, Any],
    env: dict[str, str],
    *,
    prefix: str = "[Free]",
    flow: str = "FREE",
) -> int:
    """Release stale email-pool claims left by interrupted runs."""
    enabled = _truthy(
        env.get("FREE_RESET_POOL_CLAIMS_ON_START")
        or cfg.get("free_register", {}).get("reset_pool_claims_on_start"),
        default=True,
    )
    if not enabled:
        return 0

    source_key = resolve_flow_mail_source(cfg, env, flow)
    if source_key == "moemail":
        return 0

    mail_cfg = resolve_free_mail_config(cfg, source_key)
    store = _build_free_pool_store(source_key, mail_cfg, flow=flow)
    lines = [line for line in store.in_progress_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        return 0

    store.in_progress_file.write_text("", encoding="utf-8")
    log(f"{prefix} 已清理上次中断遗留的邮箱池处理中记录 {len(lines)} 条: {store.in_progress_file}")
    return len(lines)


def _require_hotmail_tokens(account: MailAccount) -> MailAccount:
    if account.mail_url:
        return account
    if account.client_id and account.refresh_token:
        return account
    raise FreeRegisterError("Hotmail/Outlook 账号池格式需要: email----password----client_id----refresh_token")


async def _release_free_pool_claim(mail_source: FreeMailSource) -> None:
    store = mail_source.store
    if not store:
        return
    store.finish_claim(mail_source.account.email)


async def _finalize_free_pool_account(
    mail_source: FreeMailSource,
    *,
    success: bool,
    prefix: str,
    failure_reason: str = "",
) -> None:
    store = mail_source.store
    if not store:
        return
    account = mail_source.account
    if success:
        store.complete(account.email)
        store.finish_claim(account.email)
        log(f"{prefix} 邮箱池账号已标记成功: {account.email}")
        return
    store.save_failed(account.email, failure_reason or "failed")
    store.return_to_pool(account)
    log(f"{prefix} 邮箱池账号已退回/标记失败: {account.email}")


def normalize_register_mode(mode: str | None) -> str:
    raw = str(mode or "").strip().lower()
    aliases = {
        "phone": "phone",
        "mobile": "phone",
        "email": "email",
        "email-only": "email",
        "email_only": "email",
        "emailonly": "email",
    }
    normalized = aliases.get(raw)
    if not normalized:
        raise FreeRegisterError(f"invalid register_mode: {mode}")
    return normalized


def classify_free_register_error(exc: Exception) -> str:
    text = str(exc)
    low = text.lower()
    if "invalid register_mode" in low:
        return "invalid_mode"
    if "邮箱池为空" in text or "无可领取账号" in text or "mail pool" in low or "pool empty" in low:
        return "mail_pool_empty"
    if "email already registered" in low:
        return "email_already_registered"
    if "超时" in text or "timeout" in low or "timed out" in low:
        return "timeout"
    if "oauth callback missing code" in low:
        return "oauth_callback_missing_code"
    return "unknown"


def free_output_paths(root: str | Path = FREE_OUTPUT_ROOT) -> FreeOutputPaths:
    base = resolve_path(root)
    return FreeOutputPaths(root=base, account_file=base / FREE_ACCOUNT_FILE, cpa_dir=base / FREE_CPA_DIR)


def ensure_free_output_dirs(root: str | Path = FREE_OUTPUT_ROOT) -> FreeOutputPaths:
    paths = free_output_paths(root)
    paths.cpa_dir.mkdir(parents=True, exist_ok=True)
    paths.account_file.parent.mkdir(parents=True, exist_ok=True)
    if not paths.account_file.exists():
        paths.account_file.write_text("", encoding="utf-8")
    return paths


def resolve_flow_mail_source(cfg: dict[str, Any], env: dict[str, str], flow: str) -> str:
    flow_key = f"{flow.upper()}_MAIL_SOURCE"
    flow_specific = (env.get(flow_key) or "").strip().lower()
    if flow_specific:
        return normalize_mail_source(flow_specific)
    flow_defaults = {"CURSOR": "hotmail"}
    flow_default = flow_defaults.get(flow.upper())
    if flow_default:
        return normalize_mail_source(flow_default)
    value = (env.get("MAIL_SOURCE") or "").strip().lower()
    if value:
        return normalize_mail_source(value)
    return normalize_mail_source(
        str(cfg.get("mail", {}).get("active_source") or cfg.get("mail", {}).get("source") or "moemail")
    )


def normalize_mail_source(value: str) -> str:
    aliases = {
        "hotmail": "hotmail",
        "hotmail_graph": "hotmail",
        "moemail": "moemail",
        "icloud": "icloud_query",
        "icloud_query": "icloud_query",
    }
    normalized = aliases.get((value or "").strip().lower())
    if not normalized:
        raise FreeRegisterError(f"不支持的邮箱源: {value}")
    return normalized


def domain_mode_for_flow(env: dict[str, str], flow: str = "FREE") -> str:
    value = (env.get(f"{flow.upper()}_MOEMAIL_DOMAIN_MODE") or env.get("MOEMAIL_DOMAIN_MODE") or "random").strip().lower()
    if value in {"fixed", "指定", "固定"}:
        return "fixed"
    if value in {"random", "随机"}:
        return "random"
    if value in {"rotate", "round_robin", "轮询"}:
        return "rotate"
    return "random"


def select_moemail_domains(cfg: dict[str, Any], env: dict[str, str], flow: str = "FREE") -> list[str]:
    domains = split_domains(env.get(f"{flow.upper()}_MOEMAIL_DOMAIN_WHITELIST") or env.get("MOEMAIL_DOMAIN_WHITELIST") or cfg.get("mail", {}).get("moemail_domains"))
    mode = domain_mode_for_flow(env, flow)
    fixed = (env.get(f"{flow.upper()}_MOEMAIL_FIXED_DOMAIN") or env.get("MOEMAIL_FIXED_DOMAIN") or "").strip().lower().lstrip("@")
    if mode == "fixed" and fixed:
        if fixed in domains:
            return [fixed]
        raise FreeRegisterError(f"MoeMail 固定域名不在可用白名单中: {fixed}")
    if mode == "random":
        return [secrets.choice(domains)]
    return domains


def _extract_account_id_from_jwt(token: str) -> str:
    """从 access_token 或 id_token 的 JWT payload 中提取 chatgpt_account_id。"""
    import base64
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return ""
        payload_b64 = parts[1]
        # 补齐 base64 padding
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        # 优先从 auth claims 取
        auth_claims = payload.get("https://api.openai.com/auth", {})
        account_id = auth_claims.get("chatgpt_account_id", "")
        if account_id:
            return str(account_id)
        # 回退：从 id_token 的嵌套结构取
        if isinstance(auth_claims, dict):
            for key in ("account_id", "chatgpt_account_user_id"):
                val = auth_claims.get(key, "")
                if val:
                    return str(val)
    except Exception:
        pass
    return ""


def cpa_token_payload(bundle: dict[str, Any], email: str) -> dict[str, Any]:
    now = time.time()
    expired = bundle.get("expired") or bundle.get("expires") or now + 3600
    try:
        expired_value = float(expired)
    except (TypeError, ValueError):
        expired_value = now + 3600
    account_id = bundle.get("account_id", "")
    if not account_id:
        # 从 access_token JWT 解析 account_id
        account_id = _extract_account_id_from_jwt(bundle.get("access_token", ""))
    if not account_id:
        # 从 id_token JWT 解析 account_id
        account_id = _extract_account_id_from_jwt(bundle.get("id_token", ""))
    return {
        "access_token": bundle.get("access_token", ""),
        "account_id": account_id,
        "disabled": False,
        "email": bundle.get("email") or email,
        "expired": iso_from_epoch(expired_value),
        "id_token": bundle.get("id_token", ""),
        "last_refresh": iso_from_epoch(now),
        "refresh_token": bundle.get("refresh_token", ""),
        "type": "codex",
    }


def iso_from_epoch(value: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(value))


def _fetch_account_id_from_api(access_token: str) -> str:
    """调 ChatGPT /backend-api/me 接口获取 account_id（org-xxx 格式）。

    新注册账号首次 OAuth 时 JWT 里可能没有 chatgpt_account_id，
    但 /me 接口的 orgs.data 里会有。
    """
    import requests

    try:
        resp = requests.get(
            "https://chatgpt.com/backend-api/me",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code != 200:
            return ""
        data = resp.json()
        orgs = data.get("orgs", {})
        if isinstance(orgs, dict):
            org_list = orgs.get("data", [])
            if isinstance(org_list, list):
                for org in org_list:
                    if isinstance(org, dict):
                        aid = org.get("account_id") or org.get("id") or ""
                        if aid:
                            return str(aid)
        elif isinstance(orgs, list):
            for org in orgs:
                if isinstance(org, dict):
                    aid = org.get("account_id") or org.get("id") or ""
                    if aid:
                        return str(aid)
        accounts = data.get("accounts", {})
        if isinstance(accounts, dict):
            for val in accounts.values():
                if isinstance(val, dict):
                    acc = val.get("account", {})
                    if isinstance(acc, dict) and acc.get("account_id"):
                        return str(acc["account_id"])
    except Exception:
        pass
    return ""


def write_free_outputs(account: MailAccount, token_bundle: dict[str, Any], root: str | Path = FREE_OUTPUT_ROOT) -> dict[str, Path]:
    """写入 account.txt 和 codex-*.json。

    调用方应在 ``_ACCOUNT_WRITE_LOCK`` 保护下调用，避免并发写入互相覆盖。
    如果 token_bundle 缺少 account_id，会尝试调 /backend-api/me 补全。
    """
    # 补全 account_id
    if not token_bundle.get("account_id"):
        at = token_bundle.get("access_token", "")
        if at:
            fetched_id = _fetch_account_id_from_api(at)
            if fetched_id:
                token_bundle["account_id"] = fetched_id
                log(f"[Free] 从 /me 接口补全 account_id={fetched_id}")

    paths = ensure_free_output_dirs(root)
    email = account.email.strip()
    if account.password:
        account_line = f"{email}----{account.password}----{account.code_address}"
    else:
        account_line = f"{email}----{account.code_address}"
    existing = paths.account_file.read_text(encoding="utf-8", errors="ignore") if paths.account_file.exists() else ""
    lines = [line for line in existing.splitlines() if not line.lower().startswith(f"{email.lower()}----")]
    lines.append(account_line)
    paths.account_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    token_path = paths.cpa_dir / f"codex-{safe_filename(email)}-free.json"
    token_path.write_text(json.dumps(cpa_token_payload(token_bundle, email), ensure_ascii=False, indent=2), encoding="utf-8")
    return {"account": paths.account_file, "token": token_path}


async def create_free_mail_account(
    cfg: dict[str, Any],
    env: dict[str, str],
    *,
    worker_id: int | str = 1,
    flow: str = "FREE",
) -> FreeMailSource:
    source_key = resolve_flow_mail_source(cfg, env, flow)
    mail_cfg = resolve_free_mail_config(cfg, source_key)
    if source_key == "moemail":
        base_url = (env.get("MOEMAIL_BASE_URL") or mail_cfg.get("moemail_base_url") or "").strip()
        api_key = (env.get("MOEMAIL_API_KEY") or mail_cfg.get("moemail_api_key") or "").strip()
        if not base_url or not api_key:
            raise FreeRegisterError("需要配置 MOEMAIL_BASE_URL / MOEMAIL_API_KEY")
        domains = select_moemail_domains(cfg, env, flow)
        created = await create_moemail_accounts(
            base_url=base_url,
            api_key=api_key,
            count=1,
            domains=domains,
            prefix=(
                env.get(f"{flow.upper()}_MOEMAIL_CREATE_PREFIX")
                or env.get("FREE_MOEMAIL_CREATE_PREFIX")
                or env.get("MOEMAIL_CREATE_PREFIX")
                or mail_cfg.get("moemail_create_prefix", "cursor")
            ),
            mode=env.get("FREE_MOEMAIL_CREATE_MODE") or env.get("MOEMAIL_CREATE_MODE") or mail_cfg.get("moemail_create_mode", "human"),
            batch_name=f"{flow.lower()}-{int(time.time())}",
            expiry_time_ms=0,
        )
        if not created:
            raise FreeRegisterError("MoeMail 未创建到可用邮箱")
        item = created[0]
        account = parse_mail_line(item.mail_line) or MailAccount(
            email=item.email,
            mail_url=extract_code_address(item.mail_line),
            raw=item.mail_line,
        )
        if not account.mail_url:
            raise FreeRegisterError(f"MoeMail 导出行缺少接码地址: {item.email}")
        return FreeMailSource(source_key=source_key, account=account, mail_cfg=mail_cfg)

    store = _build_free_pool_store(source_key, mail_cfg, flow=flow)
    async with _FREE_POOL_LOCK:
        account = store.claim_next(worker_id)
    if not account:
        pending = store.pending_count()
        if pending > 0:
            raise FreeRegisterError(
                f"邮箱池无可领取账号: {mail_cfg.get('accounts_file')} "
                f"({pending} 行仍在池内，但都被 in_progress 标记为处理中)"
            )
        raise FreeRegisterError(f"邮箱池为空: {mail_cfg.get('accounts_file')}")
    if source_key == "hotmail":
        account = _require_hotmail_tokens(account)
    if source_key == "icloud_query" and not account.mail_url:
        raise FreeRegisterError("iCloud 查询邮箱格式需要: email----查询码")
    return FreeMailSource(source_key=source_key, account=account, mail_cfg=mail_cfg, store=store)


def extract_code_address(mail_line: str) -> str:
    parts = [part.strip() for part in str(mail_line or "").split("----")]
    if len(parts) >= 2 and parts[1]:
        return parts[1]
    return mail_line.strip()


def generate_free_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "Lc" + "".join(secrets.choice(alphabet) for _ in range(16)) + "9!"


def generate_free_profile(age_min: int, age_max: int) -> tuple[str, str]:
    first_names = [
        "Aaron", "Adam", "Alex", "Andrew", "Brian", "Caleb", "Chris", "Daniel",
        "David", "Eric", "Ethan", "Henry", "Jack", "Jason", "Kevin", "Leo",
        "Lucas", "Mark", "Nathan", "Noah", "Ryan", "Samuel", "Sean", "Thomas",
        "Austin", "Blake", "Carter", "Dylan", "Evan", "Felix", "Gavin", "Hunter",
    ]
    last_names = [
        "Adams", "Baker", "Bennett", "Carter", "Clark", "Cooper", "Davis",
        "Edwards", "Evans", "Foster", "Gray", "Hall", "Howard", "King",
        "Lewis", "Martin", "Miller", "Nelson", "Parker", "Reed", "Scott",
        "Taylor", "Turner", "Walker", "Brooks", "Collins", "Hayes", "Morgan",
    ]
    middle_initials = list(string.ascii_uppercase)
    if secrets.randbelow(100) < 35:
        full_name = f"{secrets.choice(first_names)} {secrets.choice(middle_initials)} {secrets.choice(last_names)}"
    else:
        full_name = f"{secrets.choice(first_names)} {secrets.choice(last_names)}"
    age = str(secrets.randbelow(max(1, age_max - age_min + 1)) + age_min)
    return full_name, age


def random_birth_date(age: int) -> str:
    year = max(1900, time.localtime().tm_year - int(age))
    month = secrets.randbelow(12) + 1
    day = secrets.randbelow(28) + 1
    return f"{year:04d}-{month:02d}-{day:02d}"


async def run_free_register_once(
    cfg: dict[str, Any],
    *,
    sms_selection: dict[str, object] | None,
    worker_id: int = 1,
    proxy: str | None = None,
    output_root: str | Path = FREE_OUTPUT_ROOT,
) -> bool:
    if not sms_selection:
        raise FreeRegisterError("Free 注册必须启用手机号接码，请在设置里打开 FREE_SMS_ENABLED")
    env = load_env(".env")
    mail_source = await create_free_mail_account(cfg, env)
    account = mail_source.account
    full_name, age = generate_free_profile(int(cfg["register_profile"]["age_min"]), int(cfg["register_profile"]["age_max"]))
    profile = FreeProfile(full_name=full_name, age=age, password=generate_free_password(), birth_date=random_birth_date(int(age)))
    register_account = MailAccount(
        email=account.email,
        password=profile.password,
        client_id=account.client_id,
        refresh_token=account.refresh_token,
        mail_url=account.mail_url,
        raw=account.raw,
    )
    sms_selection = {**sms_selection, "password": register_account.password, "defer_sms_complete": True}
    prefix = f"[free-{worker_id:02d}][{account.email}]"
    mail_cfg = {**cfg.get("mail", {}), "source": "moemail"}
    mail_provider = MailProvider(
        source=mail_cfg["source"],
        timeout_sec=int(mail_cfg.get("code_timeout_sec", 150)),
        poll_interval_sec=int(mail_cfg.get("poll_interval_sec", 5)),
        log_prefix=prefix,
    )
    browser_cfg = cfg.get("browser", {})
    profile_dir = resolve_path("profiles") / f"free_{safe_filename(account.email)}"
    keep_profile = bool(cfg.get("free_register", {}).get("keep_profile_on_failure", False))
    session = BrowserSession(
        profile_dir=profile_dir,
        headless=bool(browser_cfg.get("headless", False)),
        slow_mo=int(browser_cfg.get("slow_mo", 80)),
        timeout_ms=int(browser_cfg.get("timeout_ms", 60000)),
        proxy=proxy,
        fingerprint_seed=account.email,
    )
    success = False
    try:
        await session.__aenter__()
        try:
            page = await session.current_page()
            flow = FreeBrowserFlow(page, prefix)
            await phase1_phone_register(flow, sms_selection, profile, prefix)
            token_bundle = await phase2_bind_and_get_token(flow, register_account, mail_provider, sms_selection, profile, prefix)
            async with _ACCOUNT_WRITE_LOCK:
                write_free_outputs(register_account, token_bundle, output_root)
            await finalize_free_sms_activation(sms_selection, success=True, prefix=prefix)
            log(f"{prefix} Free 注册成功，已写入 {free_output_paths(output_root).root}")
            success = True
            return True
        except Exception as exc:
            try:
                await save_free_debug_page(await session.current_page(), prefix, "free_register_failure")
            except Exception:
                pass
            await finalize_free_sms_activation(sms_selection, success=False, prefix=prefix)
            log(
                f"{prefix} Free 注册失败: {exc.__class__.__name__}: {exc}\n"
                f"{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()}"
            )
            return False
        finally:
            await session.__aexit__(None, None, None)
    finally:
        if not success and not keep_profile:
            try:
                shutil.rmtree(profile_dir, ignore_errors=True)
            except Exception:
                pass


async def phase1_phone_register(flow: FreeBrowserFlow, sms_selection: dict[str, object], profile: FreeProfile, prefix: str) -> None:
    from .hero_sms_provider import PhoneCountry

    log(f"{prefix}\n=========================================")
    log(f"{prefix} [阶段1] 开始 ChatGPT 手机号注册流程")
    log(f"{prefix} =========================================")
    country = sms_selection.get("country")
    if not isinstance(country, PhoneCountry):
        raise FreeRegisterError("Free 注册缺少接码国家配置")

    await flow.navigate_to_signup()

    provider = create_sms_provider_from_selection(sms_selection)
    operator = sms_selection.get("operator")
    operator_value = str(getattr(operator, "operator", "") or "").strip()
    # 5sim 不接受空 operator，必须传 "any"；HeroSMS/Grizzly 空串表示 "任何运营商"
    provider_name = str(sms_selection.get("provider") or "herosms").lower()
    if provider_name in {"fivesim", "5sim"} and not operator_value:
        operator_value = "any"
    service = str(sms_selection.get("service") or ("openai" if provider_name in {"fivesim", "5sim"} else "dr")).strip()
    # 5sim 用 slug；HeroSMS/Grizzly 用 hero_sms_country int
    country_arg: object = country if provider_name in {"fivesim", "5sim"} else country.hero_sms_country
    activation = None
    number_used = False

    try:
        activation = await asyncio.to_thread(provider.get_number, service, country_arg, operator=operator_value)
        sms_selection["last_phone"] = activation.phone_number
        sms_selection["last_activation"] = activation
        await asyncio.to_thread(provider.mark_ready, activation.activation_id)

        await flow.select_country(country.dial_code, country.name, country.iso_code)
        local_number = flow.get_local_phone_number(activation.phone_number, country)
        await flow.enter_phone(local_number)
        number_used = True

        completed = await flow.complete_profile(
            profile,
            lambda: poll_free_sms_code(provider, activation.activation_id, sms_selection),
        )
        if not completed:
            raise FreeRegisterError("阶段1失败：注册资料填写未完成")

        log(f"{prefix} [阶段1] ChatGPT 注册流程完成！")
    except Exception:
        if activation and not number_used:
            await asyncio.to_thread(provider.cancel, activation.activation_id)
            sms_selection.pop("last_activation", None)
        raise


async def phase2_bind_and_get_token(
    flow: FreeBrowserFlow,
    account: MailAccount,
    mail_provider: MailProvider,
    sms_selection: dict[str, object],
    profile: FreeProfile,
    prefix: str,
) -> dict[str, Any]:
    """合并后的阶段 2：一次 OAuth 完成绑邮箱 + 拿 token。

    流程：手机号登录 → add-email 绑邮箱 → email-verification → consent → callback → exchange_code
    比原来的 phase2 + phase3 少一次完整 OAuth 跳转（省 25-35 秒）。
    """
    log(f"{prefix}\n=========================================")
    log(f"{prefix} [阶段2] Codex OAuth（绑邮箱 + 获取 Token）")
    log(f"{prefix} =========================================")
    code_verifier, code_challenge = get_oauth_rt._generate_pkce()
    auth_url = get_oauth_rt._build_auth_url(code_challenge, secrets.token_urlsafe(16))
    since = now_utc()
    bad_mail_codes: set[str] = set()
    log(f"{prefix} [阶段2] OAuth URL: {auth_url[:100]}...")
    await flow.navigate_to_oauth(auth_url)
    callback_url = await flow.oauth_login_and_authorize(
        {
            "loginMethod": "phone",
            "stopAfterEmailBound": False,
            "phone": str(sms_selection.get("last_phone") or ""),
            "phoneCountry": sms_selection.get("country"),
            "email": account.email,
            "password": profile.password,
            "fullName": profile.full_name,
            "age": profile.age,
            "birthDate": profile.birth_date,
            "redirectUri": get_oauth_rt.CODEX_REDIRECT_URI,
            "onSmsNeeded": lambda: poll_existing_free_sms_code(sms_selection),
            "onEmailCodeNeeded": lambda: wait_free_mail_code(mail_provider, account, since, bad_mail_codes),
        }
    )
    auth_code = get_oauth_rt.capture_code_from_url(callback_url)
    if not auth_code:
        raise FreeRegisterError(f"OAuth callback missing code: {callback_url}")
    log(f"{prefix} [阶段2] OAuth 已捕获授权码，开始换取 free token")
    return await asyncio.to_thread(get_oauth_rt.exchange_code, auth_code, code_verifier, fallback_email=account.email)


async def poll_free_sms_code(provider, activation_id: int, sms_selection: dict[str, object]) -> str:
    env = load_env(".env")
    interval = float(env.get("FREE_SMS_POLL_INTERVAL") or sms_selection.get("poll_interval") or 5.0)
    max_attempts = int(env.get("FREE_SMS_MAX_ATTEMPTS") or 36)
    try:
        code = await poll_sms_code_excluding(
            provider,
            activation_id,
            interval=interval,
            max_attempts=max_attempts,
            exclude={str(sms_selection.get("last_sms_code") or "").strip()} - {""},
        )
    except Exception:
        activation = sms_selection.get("last_activation")
        if activation and getattr(activation, "activation_id", None) == activation_id:
            sms_selection.pop("last_activation", None)
        raise
    sms_selection["last_sms_code"] = code
    return code


async def poll_existing_free_sms_code(sms_selection: dict[str, object]) -> str:
    activation = sms_selection.get("last_activation")
    if not activation:
        raise FreeRegisterError("需要短信验证码，但当前没有可用激活")
    provider = create_sms_provider_from_selection(sms_selection)
    return await poll_free_sms_code(provider, activation.activation_id, sms_selection)


async def wait_free_mail_code(mail_provider: MailProvider, account: MailAccount, since, bad_codes: set[str]) -> str:
    return await mail_provider.wait_code(account, since, bad_codes)


async def finalize_free_sms_activation(sms_selection: dict[str, object], *, success: bool, prefix: str) -> None:
    activation = sms_selection.pop("last_activation", None)
    if not activation:
        return
    provider = create_sms_provider_from_selection(sms_selection)
    try:
        if success:
            await asyncio.to_thread(provider.complete, activation.activation_id)
            log(f"{prefix} [SMS] Free 注册链路成功，已完成短信激活")
        else:
            await asyncio.to_thread(provider.cancel, activation.activation_id)
            log(f"{prefix} [SMS] Free 注册链路失败，已取消短信激活")
    except Exception as exc:
        log(f"{prefix} [SMS] 短信激活收尾失败: {exc}")


def create_sms_provider_from_selection(sms_selection: dict[str, object]):
    from .grizzly_sms_provider import GrizzlySMSProvider
    from .hero_sms_provider import HeroSMSProvider
    from .fivesim_sms_provider import FiveSimProvider

    provider_name = str(sms_selection.get("provider") or "herosms").lower()
    api_key = str(sms_selection.get("api_key") or "").strip()
    if provider_name == "grizzly":
        return GrizzlySMSProvider(api_key)
    if provider_name in {"fivesim", "5sim"}:
        return FiveSimProvider(api_key)
    return HeroSMSProvider(api_key)


async def poll_sms_code_excluding(provider, activation_id: int, *, interval: float, max_attempts: int, exclude: set[str]) -> str:
    for attempt in range(1, max(1, max_attempts) + 1):
        received, code = await asyncio.to_thread(provider.get_status, activation_id)
        normalized = str(code or "").strip()
        if received and normalized and normalized not in exclude:
            log(f"[SMS] 拉取到新短信验证码: {normalized}")
            return normalized
        if received and normalized in exclude:
            log(f"[SMS] 忽略已使用短信验证码: {normalized} ({attempt}/{max_attempts})")
        else:
            log(f"[SMS] 暂未收到新短信验证码 ({attempt}/{max_attempts})")
        await asyncio.sleep(max(1.0, interval))
    await asyncio.to_thread(provider.cancel, activation_id)
    raise TimeoutError(f"短信验证码超时（等待 {int(interval * max_attempts)} 秒），已取消激活")


async def save_free_debug_page(page, prefix: str, name: str) -> None:
    try:
        out = resolve_path("output/free注册/debug")
        out.mkdir(parents=True, exist_ok=True)
        safe = safe_filename(prefix)[-80:]
        base = out / f"{int(time.time())}_{safe}_{name}"
        text = [f"URL: {page.url}"]
        try:
            text.append(await page.locator("body").inner_text(timeout=1500))
        except Exception:
            pass
        (base.with_suffix(".txt")).write_text("\n\n".join(text), encoding="utf-8")
        await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        log(f"{prefix} [debug] 已保存 Free 注册现场: {base}")
    except Exception:
        pass


async def run_free_register_once_email(
    cfg: dict[str, Any],
    *,
    sms_selection: dict[str, object] | None,
    worker_id: int = 1,
    proxy: str | None = None,
    output_root: str | Path = FREE_OUTPUT_ROOT,
) -> bool:
    """邮箱优先注册流程：邮箱注册 → 邮箱验证 → 手机号验证 → about-you → OAuth 拿 token。

    比手机号注册方案快 ~30s，因为注册完成后账号已有邮箱，OAuth 阶段只需邮箱+密码登录。
    风险：MoeMail 域名在注册入口就暴露，可能被 OpenAI 风控。
    """
    if not sms_selection:
        raise FreeRegisterError("Free 注册必须启用手机号接码，请在设置里打开 FREE_SMS_ENABLED")
    env = load_env(".env")
    mail_source = await create_free_mail_account(cfg, env)
    account = mail_source.account
    full_name, age = generate_free_profile(int(cfg["register_profile"]["age_min"]), int(cfg["register_profile"]["age_max"]))
    profile = FreeProfile(full_name=full_name, age=age, password=generate_free_password(), birth_date=random_birth_date(int(age)))
    register_account = MailAccount(
        email=account.email,
        password=profile.password,
        client_id=account.client_id,
        refresh_token=account.refresh_token,
        mail_url=account.mail_url,
        raw=account.raw,
    )
    sms_selection = {**sms_selection, "password": register_account.password, "defer_sms_complete": True}
    prefix = f"[free-{worker_id:02d}][{account.email}]"
    mail_cfg = {**cfg.get("mail", {}), "source": "moemail"}
    mail_provider = MailProvider(
        source=mail_cfg["source"],
        timeout_sec=int(mail_cfg.get("code_timeout_sec", 150)),
        poll_interval_sec=int(mail_cfg.get("poll_interval_sec", 5)),
        log_prefix=prefix,
    )
    browser_cfg = cfg.get("browser", {})
    profile_dir = resolve_path("profiles") / f"free_{safe_filename(account.email)}"
    keep_profile = bool(cfg.get("free_register", {}).get("keep_profile_on_failure", False))
    session = BrowserSession(
        profile_dir=profile_dir,
        headless=bool(browser_cfg.get("headless", False)),
        slow_mo=int(browser_cfg.get("slow_mo", 80)),
        timeout_ms=int(browser_cfg.get("timeout_ms", 60000)),
        proxy=proxy,
        fingerprint_seed=account.email,
    )
    success = False
    try:
        await session.__aenter__()
        try:
            page = await session.current_page()
            flow = FreeBrowserFlow(page, prefix)
            # 阶段 1：邮箱注册（含手机号验证）
            await phase1_email_register(flow, register_account, mail_provider, sms_selection, profile, prefix)
            # 阶段 2：OAuth 直接用邮箱+密码拿 token（账号已有邮箱，无需再绑）
            token_bundle = await phase2_email_oauth_token(flow, register_account, mail_provider, sms_selection, profile, prefix)
            async with _ACCOUNT_WRITE_LOCK:
                write_free_outputs(register_account, token_bundle, output_root)
            await finalize_free_sms_activation(sms_selection, success=True, prefix=prefix)
            log(f"{prefix} Free 注册成功（邮箱优先），已写入 {free_output_paths(output_root).root}")
            success = True
            return True
        except Exception as exc:
            try:
                await save_free_debug_page(await session.current_page(), prefix, "free_email_register_failure")
            except Exception:
                pass
            await finalize_free_sms_activation(sms_selection, success=False, prefix=prefix)
            log(
                f"{prefix} Free 注册失败（邮箱优先）: {exc.__class__.__name__}: {exc}\n"
                f"{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()}"
            )
            return False
        finally:
            await session.__aexit__(None, None, None)
    finally:
        if not success and not keep_profile:
            try:
                shutil.rmtree(profile_dir, ignore_errors=True)
            except Exception:
                pass


async def phase1_email_register(
    flow: FreeBrowserFlow,
    account: MailAccount,
    mail_provider: MailProvider,
    sms_selection: dict[str, object],
    profile: FreeProfile,
    prefix: str,
) -> None:
    """邮箱优先注册：邮箱 → 邮箱验证码 → 设密码 → about-you → 手机号+SMS → 完成帐户创建。"""
    from .hero_sms_provider import PhoneCountry

    log(f"{prefix}\n=========================================")
    log(f"{prefix} [阶段1] 邮箱优先注册 ChatGPT")
    log(f"{prefix} =========================================")

    country = sms_selection.get("country")
    if not isinstance(country, PhoneCountry):
        raise FreeRegisterError("Free 注册缺少接码国家配置")

    since = now_utc()
    bad_mail_codes: set[str] = set()

    # 1. 导航到注册页并填入邮箱
    await flow.navigate_to_signup_email(account.email)

    # 2. 等待邮箱验证码并填入（超时则尝试重发一次）
    log(f"{prefix} [阶段1] 等待邮箱验证码...")
    try:
        email_code = await mail_provider.wait_code(account, since, bad_mail_codes)
    except TimeoutError:
        log(f"{prefix} [阶段1] 验证码超时，尝试点击重发...")
        resent = await flow.click_resend_code()
        if not resent:
            raise
        since = now_utc()
        email_code = await mail_provider.wait_code(account, since, bad_mail_codes)
    await flow.enter_email_verification_code(email_code)
    bad_mail_codes.add(email_code)

    # 3. 验证码后可能出现"创建密码"页或直接 about-you，填入密码
    log(f"{prefix} [阶段1] 验证码后检测页面状态...")
    await flow.page.wait_for_timeout(2000)
    await flow.fill_password_if_shown(profile.password)

    # 4. about-you 页（全名+年龄）
    log(f"{prefix} [阶段1] 等待 about-you 页面...")
    await flow.page.wait_for_timeout(2000)
    await flow.fill_about_you_and_submit(profile.full_name, profile.age, profile.birth_date, "[EmailRegister]")
    log(f"{prefix} [阶段1] about-you 已填写")
    await flow.wait_until_url_leaves("about-you", timeout_ms=15_000)

    # 5. 检查是否已经注册完成（有些情况下 about-you 后直接到主页，不需要手机号）
    current_url = flow.page.url
    if "chatgpt.com" in current_url and "auth.openai.com" not in current_url:
        log(f"{prefix} [阶段1] 邮箱注册完成（无需手机号验证）！")
        return

    # 5. 手机号验证（仅在 OpenAI 要求时才拉号）
    log(f"{prefix} [阶段1] 检测到需要手机号验证，开始拉号...")
    provider = create_sms_provider_from_selection(sms_selection)
    operator = sms_selection.get("operator")
    operator_value = str(getattr(operator, "operator", "") or "").strip()
    provider_name = str(sms_selection.get("provider") or "herosms").lower()
    if provider_name in {"fivesim", "5sim"} and not operator_value:
        operator_value = "any"
    service = str(sms_selection.get("service") or ("openai" if provider_name in {"fivesim", "5sim"} else "dr")).strip()
    country_arg: object = country if provider_name in {"fivesim", "5sim"} else country.hero_sms_country

    activation = await asyncio.to_thread(provider.get_number, service, country_arg, operator=operator_value)
    sms_selection["last_phone"] = activation.phone_number
    sms_selection["last_activation"] = activation
    await asyncio.to_thread(provider.mark_ready, activation.activation_id)
    number_used = False

    try:
        # 6. 选国家并输入号码
        await flow.select_country(country.dial_code, country.name, country.iso_code)
        local_number = flow.get_local_phone_number(activation.phone_number, country)
        await flow.enter_phone(local_number)
        number_used = True

        # 7. 后续步骤（SMS 验证码、可能的额外页面）交给 complete_profile
        completed = await flow.complete_profile(
            profile,
            lambda: poll_free_sms_code(provider, activation.activation_id, sms_selection),
            skip_about_you=True,
        )
        if not completed:
            raise FreeRegisterError("邮箱注册阶段1失败：注册资料填写未完成")
        log(f"{prefix} [阶段1] 邮箱注册完成！")
    except Exception:
        if activation and not number_used:
            await asyncio.to_thread(provider.cancel, activation.activation_id)
            sms_selection.pop("last_activation", None)
        raise


async def phase2_email_oauth_token(
    flow: FreeBrowserFlow,
    account: MailAccount,
    mail_provider: MailProvider,
    sms_selection: dict[str, object],
    profile: FreeProfile,
    prefix: str,
) -> dict[str, Any]:
    """邮箱+密码走 OAuth 拿 token。Phase1 已设密码，这里直接用密码登录。

    OAuth 阶段可能要求绑手机号（/add-phone），此时才 lazy 拉号。
    """
    from .hero_sms_provider import PhoneCountry

    log(f"{prefix}\n=========================================")
    log(f"{prefix} [阶段2] Codex OAuth（邮箱+密码登录获取 Token）")
    log(f"{prefix} =========================================")

    country = sms_selection.get("country")

    async def lazy_pull_sms() -> str:
        """OAuth 要求手机验证时才拉号，避免浪费接码费。"""
        await _ensure_phone_pulled()
        return await poll_existing_free_sms_code(sms_selection)

    async def _ensure_phone_pulled() -> None:
        """确保已拉号（只拉号不等验证码），供 has_phone_form 和 contact-verification 共用。"""
        if not sms_selection.get("last_activation") and isinstance(country, PhoneCountry):
            log(f"{prefix} [阶段2] OAuth 要求手机验证，现在拉号...")
            provider = create_sms_provider_from_selection(sms_selection)
            operator = sms_selection.get("operator")
            operator_value = str(getattr(operator, "operator", "") or "").strip()
            provider_name = str(sms_selection.get("provider") or "herosms").lower()
            if provider_name in {"fivesim", "5sim"} and not operator_value:
                operator_value = "any"
            service = str(sms_selection.get("service") or ("openai" if provider_name in {"fivesim", "5sim"} else "dr")).strip()
            country_arg: object = country if provider_name in {"fivesim", "5sim"} else country.hero_sms_country
            activation = await asyncio.to_thread(provider.get_number, service, country_arg, operator=operator_value)
            sms_selection["last_phone"] = activation.phone_number
            sms_selection["last_activation"] = activation
            await asyncio.to_thread(provider.mark_ready, activation.activation_id)
            oauth_opts["phone"] = activation.phone_number

    code_verifier, code_challenge = get_oauth_rt._generate_pkce()
    auth_url = get_oauth_rt._build_auth_url(code_challenge, secrets.token_urlsafe(16))
    since = now_utc()
    bad_mail_codes: set[str] = set()
    log(f"{prefix} [阶段2] OAuth URL: {auth_url[:100]}...")
    await flow.navigate_to_oauth(auth_url)
    oauth_opts = {
        "loginMethod": "email",
        "preferEmailOtp": False,
        "stopAfterEmailBound": False,
        "phone": str(sms_selection.get("last_phone") or ""),
        "phoneCountry": sms_selection.get("country"),
        "email": account.email,
        "password": profile.password,
        "fullName": profile.full_name,
        "age": profile.age,
        "birthDate": profile.birth_date,
        "redirectUri": get_oauth_rt.CODEX_REDIRECT_URI,
        "onSmsNeeded": lazy_pull_sms,
        "onPullPhone": _ensure_phone_pulled,
        "onEmailCodeNeeded": lambda: wait_free_mail_code(mail_provider, account, since, bad_mail_codes),
    }
    callback_url = await flow.oauth_login_and_authorize(oauth_opts)
    auth_code = get_oauth_rt.capture_code_from_url(callback_url)
    if not auth_code:
        raise FreeRegisterError(f"OAuth callback missing code: {callback_url}")
    log(f"{prefix} [阶段2] OAuth 已捕获授权码，开始换取 free token")
    return await asyncio.to_thread(get_oauth_rt.exchange_code, auth_code, code_verifier, fallback_email=account.email)


async def run_free_register_many(cfg: dict[str, Any], *, count: int, workers: int, sms_selection: dict[str, object] | None, register_mode: str = "phone") -> int:
    register_mode = normalize_register_mode(register_mode)
    proxy_pool = ProxyPool(cfg.get("browser", {}).get("proxy_file", "data/proxies/proxies.txt")) if cfg.get("browser", {}).get("use_proxy") else None
    success = 0
    attempts = 0
    max_attempts = max(count * int(cfg.get("free_register", {}).get("max_attempt_multiplier", 5)), count)
    lock = asyncio.Lock()

    async def worker(worker_id: int) -> None:
        nonlocal attempts, success
        while True:
            async with lock:
                if success >= count or attempts >= max_attempts:
                    return
                attempts += 1
                attempt_no = attempts
            log(f"[free-{worker_id:02d}] 开始第 {attempt_no}/{max_attempts} 次尝试，目标成功 {success}/{count}")
            # 每次尝试都轮换代理，避免同一 worker 被单个代理绑死
            proxy = proxy_pool.pick(attempt_no) if proxy_pool else None
            worker_sms_selection = {**sms_selection} if sms_selection else None
            try:
                ok = await _run_register_once_by_mode(
                    cfg,
                    register_mode=register_mode,
                    sms_selection=worker_sms_selection,
                    worker_id=worker_id,
                    proxy=proxy,
                )
            except Exception as exc:
                err_code = classify_free_register_error(exc)
                log(f"[free-{worker_id:02d}] Free 注册尝试启动失败[{err_code}]: {exc.__class__.__name__}: {exc}")
                ok = False
            if ok:
                async with lock:
                    success += 1

    await asyncio.gather(*(worker(worker_id) for worker_id in range(1, max(1, workers) + 1)))
    log(f"Free 注册结束，成功数: {success}/{count}，尝试数: {attempts}/{max_attempts}")
    return 0 if success >= count else 1


async def _run_register_once_by_mode(
    cfg: dict[str, Any],
    *,
    register_mode: str,
    sms_selection: dict[str, object] | None,
    worker_id: int,
    proxy: str | None,
) -> bool:
    register_mode = normalize_register_mode(register_mode)
    if register_mode == "email":
        return await run_free_register_once_email(cfg, sms_selection=sms_selection, worker_id=worker_id, proxy=proxy)
    return await run_free_register_once(cfg, sms_selection=sms_selection, worker_id=worker_id, proxy=proxy)


def interactive_free_register(config_path: str, cfg: dict[str, Any], sms_selection: dict[str, object] | None, ask_positive_int) -> int:
    print()
    print("功能五：Free 注册")
    print()
    print("请选择注册方式：")
    print("  1. 手机号注册（当前稳定方案）")
    print("  2. 邮箱注册（实验性，更快但域名暴露更早）")
    while True:
        mode_choice = input("请输入选项 [1/2]: ").strip() or "1"
        if mode_choice in {"1", "2"}:
            break
        print("请输入 1 或 2。")
    register_mode = "phone" if mode_choice == "1" else "email"
    count = ask_positive_int("请输入这次要成功注册多少个", default=1)
    workers = ask_positive_int("请输入并发线程数", default=1, max_value=count)
    return asyncio.run(run_free_register_many(cfg, count=count, workers=workers, sms_selection=sms_selection, register_mode=register_mode))
