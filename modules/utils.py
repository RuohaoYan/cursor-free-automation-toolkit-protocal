from __future__ import annotations

import asyncio
import random
import re
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from modules.terminal_theme import GRAY, paint, style_timed_log


PROJECT_ROOT = Path(__file__).resolve().parents[1]

OUTPUT_FILES = {
    "flow1_success": "output/gopay_plus/flow1_success_links.txt",
    "flow1_failed": "output/gopay_plus/flow1_failed_accounts.txt",
    "flow1_in_progress": "output/gopay_plus/flow1_in_progress.txt",
    "flow2_paid_success": "output/gopay_plus/flow2_paid_success_pending_auth.txt",
    "flow2_nonzero_billing": "output/gopay_plus/flow2_nonzero_billing.txt",
}

LEGACY_OUTPUT_FILES = {
    "flow1_success": "output/success.txt",
    "flow1_failed": "output/failed.txt",
    "flow1_in_progress": "output/in_progress.txt",
    "flow2_paid_success": "output/paid_success.txt",
    "flow2_nonzero_billing": "output/nonzero_billing.txt",
}


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def migrate_output_file(new_path: str | Path, legacy_path: str | Path | None = None) -> Path:
    target = resolve_path(new_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if legacy_path:
        legacy = resolve_path(legacy_path)
        if legacy.exists():
            legacy_text = legacy.read_text(encoding="utf-8")
            target_text = target.read_text(encoding="utf-8") if target.exists() else ""
            if legacy_text.strip() and legacy_text not in target_text:
                combined = target_text.rstrip()
                if combined:
                    combined += "\n"
                combined += legacy_text.strip() + "\n"
                target.write_text(combined, encoding="utf-8")
            backup_dir = legacy.parent / "legacy_backup"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f"old_{legacy.name}"
            if backup.exists():
                backup = backup_dir / f"old_{legacy.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{legacy.suffix}"
            legacy.rename(backup)
    if not target.exists():
        target.write_text("", encoding="utf-8")
    return target


def output_file(key: str) -> str:
    return OUTPUT_FILES[key]


def migrate_known_output_files() -> None:
    for key, path in OUTPUT_FILES.items():
        migrate_output_file(path, LEGACY_OUTPUT_FILES.get(key))


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = resolve_path(path)
    with config_path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_env(path: str | Path = ".env") -> dict[str, str]:
    env_path = resolve_path(path)
    if not env_path.exists():
        return {}
    result: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def env_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(message: str) -> None:
    stamp = paint(f"[{datetime.now().strftime('%H:%M:%S')}]", GRAY)
    text = f"{stamp} {style_timed_log(message)}"
    print(text, flush=True)


def extract_code(text: str) -> str | None:
    codes = extract_codes(text)
    return codes[0] if codes else None


def extract_codes(text: str) -> list[str]:
    # Email HTML often contains CSS colors like #202123 / #353740.
    # Remove style/script/html/color tokens before OTP extraction.
    normalized = text or ""
    normalized = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", normalized)
    normalized = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", normalized)
    normalized = re.sub(r"(?is)<!--.*?-->", " ", normalized)
    normalized = re.sub(r"(?is)<[^>]+>", " ", normalized)
    normalized = re.sub(r"#[0-9a-fA-F]{6}\b", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)

    codes: list[str] = []
    for pattern in [
        r"(?:verification\s*code|one[-\s]*time\s*code|login\s*code|code)[^0-9]{0,30}(\d{6})",
        r"(?:验证码|临时验证码|登录代码|安全代码|验证代码)[^0-9]{0,30}(\d{6})",
        r"(?<!\d)(\d{6})(?!\d)",
    ]:
        for found in re.findall(pattern, normalized, flags=re.I):
            code = found if isinstance(found, str) else found[0]
            if code not in codes:
                codes.append(code)
    return codes

def random_profile(age_min: int, age_max: int) -> tuple[str, str]:
    first_names = [
        "Aaron", "Adam", "Alex", "Andrew", "Brian", "Caleb", "Chris", "Daniel",
        "David", "Eric", "Ethan", "Henry", "Jack", "Jason", "Kevin", "Leo",
        "Lucas", "Mark", "Nathan", "Noah", "Ryan", "Samuel", "Sean", "Thomas",
    ]
    last_names = [
        "Adams", "Baker", "Bennett", "Carter", "Clark", "Cooper", "Davis",
        "Edwards", "Evans", "Foster", "Gray", "Hall", "Howard", "King",
        "Lewis", "Martin", "Miller", "Nelson", "Parker", "Reed", "Scott",
        "Taylor", "Turner", "Walker",
    ]
    suffix = "".join(random.choices(string.ascii_lowercase, k=2))
    full_name = f"{random.choice(first_names)} {random.choice(last_names)} {suffix}"
    age = str(random.randint(age_min, age_max))
    return full_name, age


async def pause_for_user(reason: str) -> None:
    log(reason)
    await asyncio.to_thread(input, "Handle browser manually, then input next to continue: ")


def safe_filename(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.@-]+", "_", value)

