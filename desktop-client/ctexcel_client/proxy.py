from __future__ import annotations

import base64
import json
import re
import socket
import ssl
import struct
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

import httpx

from .config import ProxyConfig


class ProxyError(RuntimeError):
    pass


def _json_proxy_candidate(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        for item in value:
            candidate = _json_proxy_candidate(item)
            if candidate:
                return candidate
        return ""
    if not isinstance(value, dict):
        return ""

    server = value.get("server")
    if isinstance(server, str) and ":" in server and not value.get("port"):
        return server.strip()

    host = str(
        value.get("host")
        or value.get("ip")
        or value.get("server")
        or ""
    ).strip()
    port = str(value.get("port") or "").strip()
    if host and port:
        username = str(
            value.get("username")
            or value.get("user")
            or ""
        ).strip()
        password = str(
            value.get("password")
            or value.get("pass")
            or ""
        ).strip()
        auth = f"{username}:{password}@" if username else ""
        return f"{auth}{host}:{port}"

    for key in ("proxy", "data", "result", "items", "list"):
        if key in value:
            candidate = _json_proxy_candidate(value[key])
            if candidate:
                return candidate
    return ""


def parse_proxy_payload(
    payload: str,
    *,
    default_scheme: str = "socks5",
    fallback_username: str = "",
    fallback_password: str = "",
) -> dict[str, str]:
    """解析 txt/JSON 代理接口常见返回格式。"""
    raw = str(payload or "").strip()
    if not raw:
        raise ProxyError("代理接口返回为空")

    candidate = ""
    if raw.startswith(("{", "[")):
        try:
            candidate = _json_proxy_candidate(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ProxyError("代理接口返回的 JSON 格式错误") from exc
    else:
        candidate = next(
            (line.strip() for line in raw.splitlines() if line.strip()),
            "",
        )
    if not candidate:
        raise ProxyError("代理接口没有返回可用代理")

    scheme = default_scheme.strip().lower() or "socks5"
    username = fallback_username.strip()
    password = fallback_password

    # 同时兼容 host:port:user:password。
    if "://" not in candidate and "@" not in candidate:
        parts = candidate.split(":")
        if len(parts) == 4 and parts[1].isdigit():
            candidate = f"{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"

    normalized = candidate if "://" in candidate else f"{scheme}://{candidate}"
    parsed = urlsplit(normalized)
    parsed_scheme = parsed.scheme.lower()
    if parsed_scheme not in {"http", "https", "socks5"}:
        raise ProxyError(f"代理协议不受支持：{parsed_scheme or '未知'}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProxyError("代理端口格式错误") from exc
    host = parsed.hostname or ""
    if not host or not port or port < 1 or port > 65535:
        raise ProxyError("代理接口应返回 HOST:PORT")

    if parsed.username:
        username = unquote(parsed.username)
    if parsed.password:
        password = unquote(parsed.password)

    result = {"server": f"{parsed_scheme}://{host}:{port}"}
    if username:
        result["username"] = username
    if password:
        result["password"] = password
    return result


def fetch_proxy_from_api(
    config: ProxyConfig,
    *,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, str]:
    url = config.api_url.strip()
    if not url.startswith(("https://", "http://")):
        raise ProxyError("代理提取接口需要以 https:// 或 http:// 开头")
    try:
        with httpx.Client(
            timeout=max(3, int(config.api_timeout_seconds)),
            follow_redirects=True,
            trust_env=False,
            transport=transport,
            headers={
                "Accept": "text/plain, application/json",
                "User-Agent": "CTExcelApplyClient/2.0.6",
            },
        ) as client:
            response = client.get(url)
            response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise ProxyError("代理接口请求超时") from exc
    except httpx.HTTPStatusError as exc:
        raise ProxyError(
            f"代理接口返回 HTTP {exc.response.status_code}"
        ) from exc
    except httpx.RequestError as exc:
        raise ProxyError("代理接口网络连接失败") from exc
    return parse_proxy_payload(
        response.text,
        default_scheme=config.proxy_type,
        fallback_username=config.username,
        fallback_password=config.password,
    )


def resolve_proxy(
    config: ProxyConfig,
    *,
    transport: Optional[httpx.BaseTransport] = None,
) -> Optional[dict[str, str]]:
    mode = config.mode.strip().lower()
    if mode in {"", "none"}:
        return None
    if mode == "api":
        return fetch_proxy_from_api(config, transport=transport)
    if mode != "custom":
        raise ProxyError(f"未知代理模式：{config.mode}")
    result = config.playwright_proxy()
    if not result:
        raise ProxyError("请填写固定代理的地址和端口")
    return result


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise ProxyError("代理服务器在握手过程中断开连接")
        chunks.extend(chunk)
    return bytes(chunks)


def _probe_socks5(
    sock: socket.socket,
    proxy: dict[str, str],
    target_host: str,
    target_port: int,
) -> None:
    username = str(proxy.get("username") or "")
    password = str(proxy.get("password") or "")
    methods = b"\x00\x02" if username else b"\x00"
    sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
    version, method = _recv_exact(sock, 2)
    if version != 5 or method == 0xFF:
        raise ProxyError(
            "SOCKS5 代理拒绝认证；请检查当前公网 IP 白名单或代理账号密码"
        )
    if method == 0x02:
        user_bytes = username.encode("utf-8")
        pass_bytes = password.encode("utf-8")
        if not user_bytes or len(user_bytes) > 255 or len(pass_bytes) > 255:
            raise ProxyError("SOCKS5 代理账号密码格式错误")
        sock.sendall(
            b"\x01"
            + bytes([len(user_bytes)])
            + user_bytes
            + bytes([len(pass_bytes)])
            + pass_bytes
        )
        auth_version, auth_status = _recv_exact(sock, 2)
        if auth_version != 1 or auth_status != 0:
            raise ProxyError("SOCKS5 代理账号密码验证失败")
    elif method != 0x00:
        raise ProxyError("SOCKS5 代理要求客户端暂不支持的认证方式")

    host_bytes = target_host.encode("idna")
    if len(host_bytes) > 255:
        raise ProxyError("代理测试目标域名过长")
    request = (
        b"\x05\x01\x00\x03"
        + bytes([len(host_bytes)])
        + host_bytes
        + struct.pack("!H", target_port)
    )
    sock.sendall(request)
    version, status, _reserved, address_type = _recv_exact(sock, 4)
    if version != 5 or status != 0:
        raise ProxyError(
            "SOCKS5 代理拒绝访问目标网站；请检查白名单、有效期和地区节点"
        )
    if address_type == 1:
        _recv_exact(sock, 4)
    elif address_type == 3:
        _recv_exact(sock, _recv_exact(sock, 1)[0])
    elif address_type == 4:
        _recv_exact(sock, 16)
    else:
        raise ProxyError("SOCKS5 代理返回了未知地址格式")
    _recv_exact(sock, 2)


def _probe_http_connect(
    sock: socket.socket,
    proxy: dict[str, str],
    target_host: str,
    target_port: int,
) -> None:
    headers = [
        f"CONNECT {target_host}:{target_port} HTTP/1.1",
        f"Host: {target_host}:{target_port}",
        "Proxy-Connection: close",
    ]
    username = str(proxy.get("username") or "")
    password = str(proxy.get("password") or "")
    if username:
        token = base64.b64encode(
            f"{username}:{password}".encode("utf-8")
        ).decode("ascii")
        headers.append(f"Proxy-Authorization: Basic {token}")
    sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
    response = bytearray()
    while b"\r\n" not in response and len(response) < 4096:
        chunk = sock.recv(512)
        if not chunk:
            break
        response.extend(chunk)
    first_line = bytes(response).split(b"\r\n", 1)[0].decode(
        "latin-1",
        "replace",
    )
    if not re.match(r"HTTP/\d(?:\.\d)?\s+200\b", first_line):
        raise ProxyError(
            "HTTP 代理拒绝 CONNECT；请检查白名单、账号密码或代理协议"
        )


def probe_proxy_endpoint(
    proxy: dict[str, str],
    timeout: float = 8,
    *,
    target_host: str = "www.ctexcel.com",
    target_port: int = 443,
) -> None:
    """完成代理协议握手，并确认代理允许连接 CTExcel。"""
    parsed = urlsplit(str(proxy.get("server") or ""))
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProxyError("代理端口格式错误") from exc
    if not host or not port:
        raise ProxyError("代理地址格式错误")
    if parsed.scheme not in {"http", "https", "socks5"}:
        raise ProxyError("代理协议应为 HTTP、HTTPS 或 SOCKS5")
    try:
        with socket.create_connection(
            (host, port),
            timeout=max(1, timeout),
        ) as raw_socket:
            raw_socket.settimeout(max(1, timeout))
            if parsed.scheme == "socks5":
                _probe_socks5(
                    raw_socket,
                    proxy,
                    target_host,
                    target_port,
                )
                return
            if parsed.scheme == "https":
                context = ssl.create_default_context()
                with context.wrap_socket(
                    raw_socket,
                    server_hostname=host,
                ) as secure_socket:
                    _probe_http_connect(
                        secure_socket,
                        proxy,
                        target_host,
                        target_port,
                    )
                return
            _probe_http_connect(
                raw_socket,
                proxy,
                target_host,
                target_port,
            )
    except ProxyError:
        raise
    except OSError as exc:
        raise ProxyError("代理服务器连接或协议握手失败") from exc


def masked_proxy_label(proxy: Optional[dict[str, str]]) -> str:
    if not proxy:
        return "直连"
    parsed = urlsplit(str(proxy.get("server") or ""))
    host = parsed.hostname or ""
    if host.count(".") == 3:
        parts = host.split(".")
        host = f"{parts[0]}.{parts[1]}.*.*"
    elif len(host) > 8:
        host = host[:4] + "…" + host[-3:]
    return f"{parsed.scheme or 'proxy'}://{host}:{parsed.port or ''}"
