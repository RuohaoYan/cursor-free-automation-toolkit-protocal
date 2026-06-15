"""本地 HTTP CONNECT 转发：上游代理(Clash) → Kookeey 网关 → 目标站点。"""
from __future__ import annotations

import asyncio
import base64
import socket
import threading
from dataclasses import dataclass
from urllib.parse import urlparse

from .utils import log


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _parse_upstream(upstream: str) -> tuple[str, int]:
    raw = (upstream or "").strip()
    if not raw:
        raise ValueError("upstream proxy is empty")
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"upstream proxy format invalid: {upstream}")
    return parsed.hostname, parsed.port


def _basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return token


async def _read_http_headers(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    status_line = (await reader.readline()).decode("latin-1", errors="ignore").strip()
    if not status_line:
        raise ConnectionError("empty proxy response")
    parts = status_line.split(" ", 2)
    status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
    headers: dict[str, str] = {}
    while True:
        line = (await reader.readline()).decode("latin-1", errors="ignore").strip()
        if not line:
            break
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return status, headers


async def _proxy_connect(
    upstream_host: str,
    upstream_port: int,
    target_host: str,
    target_port: int,
    *,
    auth_token: str | None = None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection(upstream_host, upstream_port)
    lines = [
        f"CONNECT {target_host}:{target_port} HTTP/1.1",
        f"Host: {target_host}:{target_port}",
    ]
    if auth_token:
        lines.append(f"Proxy-Authorization: Basic {auth_token}")
    lines.extend(["", ""])
    writer.write("\r\n".join(lines).encode("latin-1"))
    await writer.drain()
    status, _ = await _read_http_headers(reader)
    if status != 200:
        raise ConnectionError(f"CONNECT {target_host}:{target_port} via upstream failed: HTTP {status}")
    return reader, writer


async def _relay(
    left_reader: asyncio.StreamReader,
    left_writer: asyncio.StreamWriter,
    right_reader: asyncio.StreamReader,
    right_writer: asyncio.StreamWriter,
) -> None:
    async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await src.read(65536)
                if not chunk:
                    break
                dst.write(chunk)
                await dst.drain()
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass
        except Exception:
            pass

    await asyncio.gather(
        pump(left_reader, right_writer),
        pump(right_reader, left_writer),
        return_exceptions=True,
    )
    for writer in (left_writer, right_writer):
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


@dataclass(frozen=True)
class ChainGateway:
    host: str
    port: int
    username: str
    password: str


class ProxyChainForwarder:
    """127.0.0.1 本地端口：浏览器连此端口，经 upstream 访问 Kookeey 再出网。"""

    def __init__(
        self,
        upstream: str,
        gateway: ChainGateway,
        *,
        bind_host: str = "127.0.0.1",
    ) -> None:
        self.upstream_host, self.upstream_port = _parse_upstream(upstream)
        self.gateway = gateway
        self.bind_host = bind_host
        self.port = _pick_free_port()
        self._auth_token = _basic_auth_header(gateway.username, gateway.password)
        self._server: asyncio.Server | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client_tasks: set[asyncio.Task[None]] = set()

    @property
    def proxy_url(self) -> str:
        return f"http://{self.bind_host}:{self.port}"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        ready = threading.Event()
        error: list[BaseException] = []

        def runner() -> None:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                loop.run_until_complete(self._start_server(ready))
                loop.run_forever()
            except BaseException as exc:  # noqa: BLE001
                error.append(exc)
                ready.set()
            finally:
                if self._loop:
                    self._loop.close()

        self._thread = threading.Thread(target=runner, name=f"proxy-chain-{self.port}", daemon=True)
        self._thread.start()
        ready.wait(timeout=5)
        if error:
            raise error[0]
        if not self._server:
            raise RuntimeError("proxy chain forwarder failed to start")

    async def _start_server(self, ready: threading.Event) -> None:
        def _on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.create_task(self._handle_client(reader, writer))
            self._client_tasks.add(task)
            task.add_done_callback(self._client_tasks.discard)

        self._server = await asyncio.start_server(
            _on_client,
            self.bind_host,
            self.port,
        )
        log(
            f"[ProxyChain] 本地链式代理 {self.proxy_url} "
            f"(upstream={self.upstream_host}:{self.upstream_port} → "
            f"{self.gateway.host}:{self.gateway.port})"
        )
        ready.set()

    async def _open_gateway_tunnel(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        # 经 Clash 建隧道到 Kookeey 网关，此处不要带 Kookeey 鉴权
        return await _proxy_connect(
            self.upstream_host,
            self.upstream_port,
            self.gateway.host,
            self.gateway.port,
        )

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = (await client_reader.readline()).decode("latin-1", errors="ignore").strip()
            if not request_line:
                return
            method, target, _ = request_line.split(" ", 2)
            headers: dict[str, str] = {}
            while True:
                line = (await client_reader.readline()).decode("latin-1", errors="ignore").strip()
                if not line:
                    break
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.strip().lower()] = value.strip()

            if method.upper() != "CONNECT":
                client_writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
                await client_writer.drain()
                return

            if ":" in target:
                target_host, target_port_s = target.rsplit(":", 1)
                target_port = int(target_port_s)
            else:
                target_host, target_port = target, 443

            gateway_reader, gateway_writer = await self._open_gateway_tunnel()
            lines = [
                f"CONNECT {target_host}:{target_port} HTTP/1.1",
                f"Host: {target_host}:{target_port}",
                f"Proxy-Authorization: Basic {self._auth_token}",
                "",
                "",
            ]
            gateway_writer.write("\r\n".join(lines).encode("latin-1"))
            await gateway_writer.drain()
            status, _ = await _read_http_headers(gateway_reader)
            if status != 200:
                client_writer.write(f"HTTP/1.1 {status} Bad Gateway\r\nConnection: close\r\n\r\n".encode())
                await client_writer.drain()
                return

            client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await client_writer.drain()
            await _relay(client_reader, client_writer, gateway_reader, gateway_writer)
        except Exception as exc:
            log(f"[ProxyChain] 转发失败: {exc.__class__.__name__}: {exc}")
            try:
                client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                await client_writer.drain()
            except Exception:
                pass

    def stop(self) -> None:
        if not self._loop or not self._server:
            return
        async def _close() -> None:
            for task in list(self._client_tasks):
                task.cancel()
            if self._client_tasks:
                await asyncio.gather(*self._client_tasks, return_exceptions=True)
            self._server.close()
            await self._server.wait_closed()
            self._loop.stop()
        try:
            asyncio.run_coroutine_threadsafe(_close(), self._loop).result(timeout=3)
        except Exception:
            pass


_forwarders: dict[str, ProxyChainForwarder] = {}
_lock = threading.Lock()


def get_chained_local_proxy_url(
    upstream: str,
    gateway: ChainGateway,
    *,
    cache_key: str,
) -> str:
    with _lock:
        existing = _forwarders.get(cache_key)
        if existing and existing._thread and existing._thread.is_alive():
            existing._auth_token = _basic_auth_header(gateway.username, gateway.password)
            existing.gateway = gateway
            return existing.proxy_url
        # 同 upstream 复用一个本地端口，仅更新 session 密码
        reuse_key = f"upstream:{upstream}|{gateway.host}:{gateway.port}|{gateway.username}"
        existing = _forwarders.get(reuse_key)
        if existing and existing._thread and existing._thread.is_alive():
            existing._auth_token = _basic_auth_header(gateway.username, gateway.password)
            existing.gateway = gateway
            _forwarders[cache_key] = existing
            return existing.proxy_url
        forwarder = ProxyChainForwarder(upstream, gateway)
        forwarder.start()
        _forwarders[cache_key] = forwarder
        _forwarders[reuse_key] = forwarder
        return forwarder.proxy_url


def reset_chained_forwarders() -> None:
    with _lock:
        for forwarder in _forwarders.values():
            forwarder.stop()
        _forwarders.clear()
