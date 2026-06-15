from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from modules.mail_provider import list_recent_messages, refresh_graph_access_token
from modules.storage import parse_mail_line
from modules.utils import PROJECT_ROOT


DEFAULT_POOL_FILES = (
    "data/hotmail/accounts_pool.txt",
    "data/hotmail/accounts.txt",
    "data/hotmail/mail_pool.txt",
)


def load_account(*, email: str, line: str, pool_file: str | None) -> tuple[str, str, str]:
    if line.strip():
        account = parse_mail_line(line.strip())
        if not account or not account.client_id or not account.refresh_token:
            raise SystemExit("账号行格式需为: email----password----client_id----refresh_token")
        return account.email, account.client_id, account.refresh_token

    target = email.strip().lower()
    if not target:
        raise SystemExit("请指定 --email 或 --line")

    candidates: list[Path] = []
    if pool_file:
        candidates.append(Path(pool_file) if Path(pool_file).is_absolute() else PROJECT_ROOT / pool_file)
    else:
        candidates.extend(PROJECT_ROOT / path for path in DEFAULT_POOL_FILES)

    for path in candidates:
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
            text = raw.strip()
            if not text or text.startswith("#"):
                continue
            account = parse_mail_line(text)
            if not account:
                continue
            if account.email.lower() != target:
                continue
            if not account.client_id or not account.refresh_token:
                raise SystemExit(f"在 {path} 找到 {account.email}，但缺少 client_id / refresh_token")
            return account.email, account.client_id, account.refresh_token

    searched = ", ".join(str(p) for p in candidates)
    raise SystemExit(f"未在账号池中找到 {email}（已搜索: {searched}）")


def format_sender(item: dict) -> str:
    return (((item.get("from") or {}).get("emailAddress") or {}).get("address") or "").strip()


def safe_print(text: str = "") -> None:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    payload = f"{text}\n".encode(encoding, errors="replace")
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(payload)
        buffer.flush()
        return
    sys.stdout.write(payload.decode(encoding, errors="replace"))
    sys.stdout.flush()


def format_preview(item: dict, max_len: int) -> str:
    text = (item.get("bodyPreview") or item.get("subject") or "").replace("\r", " ").replace("\n", " ")
    text = " ".join(text.split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


async def fetch_messages(client_id: str, refresh_token: str, top: int) -> list[dict]:
    access_token = await refresh_graph_access_token(client_id, refresh_token)
    messages = await list_recent_messages(access_token)
    return messages[:top]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="查看 Hotmail/Outlook 邮箱近期邮件列表（Microsoft Graph）")
    parser.add_argument("--email", help="邮箱地址，从账号池文件中查找凭证")
    parser.add_argument("--line", help="完整账号行: email----password----client_id----refresh_token")
    parser.add_argument("--file", dest="pool_file", help="指定账号池文件路径")
    parser.add_argument("--top", type=int, default=20, help="显示最近 N 封邮件，默认 20")
    parser.add_argument("--preview-len", type=int, default=72, help="摘要最大字符数，默认 72")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    top = max(1, int(args.top or 20))
    preview_len = max(20, int(args.preview_len or 72))

    try:
        email, client_id, refresh_token = load_account(
            email=args.email or "",
            line=args.line or "",
            pool_file=args.pool_file,
        )
    except SystemExit as exc:
        safe_print(str(exc))
        return 2

    try:
        messages = asyncio.run(fetch_messages(client_id, refresh_token, top))
    except Exception as exc:
        safe_print(f"读取邮件失败: {exc}")
        return 1

    safe_print(f"邮箱: {email}")
    safe_print(f"共读取 {len(messages)} 封（最近 {top} 封上限）")
    safe_print()

    if not messages:
        safe_print("（暂无邮件）")
        return 0

    index_width = len(str(len(messages)))
    for idx, item in enumerate(messages, start=1):
        received = item.get("receivedDateTime") or "-"
        sender = format_sender(item) or "-"
        subject = (item.get("subject") or "(无主题)").replace("\n", " ")
        preview = format_preview(item, preview_len)
        safe_print(f"{idx:>{index_width}}. [{received}]")
        safe_print(f"    发件人: {sender}")
        safe_print(f"    主题:   {subject}")
        safe_print(f"    摘要:   {preview}")
        safe_print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
