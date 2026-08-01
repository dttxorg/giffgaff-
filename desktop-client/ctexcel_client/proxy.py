from __future__ import annotations

import base64
import contextlib
from dataclasses import dataclass
import ipaddress
import json
import random
import re
import select
import socket
import socketserver
import ssl
import struct
import threading
from typing import Any, Optional
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import httpx

from .config import ProxyConfig, is_qg_proxy_api_url


class ProxyError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedProxy:
    playwright_proxy: Optional[dict[str, str]]
    public_ip: str = ""
    public_ip_error: str = ""


@dataclass(frozen=True)
class ProxyPoolLease:
    proxy: dict[str, str]
    pool_index: int
    pool_size: int
    use_number: int
    use_limit: int


PUBLIC_IP_ENDPOINTS = (
    "https://api.ipify.org?format=json",
    "https://checkip.amazonaws.com",
    "https://1.1.1.1/cdn-cgi/trace",
)

QG_PROXY_ERROR_MESSAGES = {
    "INTERNAL_ERROR": "代理平台内部异常",
    "INVALID_PARAMETER": "提取参数格式或类型错误",
    "INVALID_KEY": "API Key 不存在或已过期",
    "UNAVAILABLE_KEY": "API Key 已过期或被封禁",
    "ACCESS_DENY": "API Key 没有提取接口权限",
    "API_AUTH_DENY": "API 鉴权配置未通过",
    "KEY_BLOCK": "API Key 已被封禁",
    "REQUEST_LIMIT_EXCEEDED": "请求频率超过 60 次/分钟",
    "NO_RESOURCE_FOUND": "当前筛选条件下代理资源不足",
    "FAILED_OPERATION": "代理提取操作失败",
    "EXTRACT_LIMIT_EXCEEDED": "今日 IP 提取配额已用完",
}


def _valid_public_ip(value: str) -> str:
    candidate = str(value or "").strip()
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return ""
    if not address.is_global:
        return ""
    return str(address)


def _public_ip_from_response(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        candidate = _valid_public_ip(str(payload.get("ip") or ""))
        if candidate:
            return candidate

    text = response.text.strip()
    trace_match = re.search(r"(?m)^ip=([^\r\n]+)", text)
    if trace_match:
        candidate = _valid_public_ip(trace_match.group(1))
        if candidate:
            return candidate
    first_line = text.splitlines()[0].strip() if text else ""
    return _valid_public_ip(first_line)


def detect_public_ip(
    *,
    transport: Optional[httpx.BaseTransport] = None,
    timeout: float = 5,
) -> str:
    """通过直连 HTTPS 检测运行客户端的出口公网 IP。"""
    try:
        with httpx.Client(
            timeout=max(2, timeout),
            follow_redirects=True,
            trust_env=False,
            transport=transport,
            headers={
                "Accept": "text/plain, application/json",
                "User-Agent": "CTExcelApplyClient/2.5.9",
            },
        ) as client:
            for endpoint in PUBLIC_IP_ENDPOINTS:
                try:
                    response = client.get(endpoint)
                    response.raise_for_status()
                except httpx.HTTPError:
                    continue
                public_ip = _public_ip_from_response(response)
                if public_ip:
                    return public_ip
    except httpx.HTTPError:
        pass
    raise ProxyError("当前出口公网 IP 检测失败")


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
        parts = candidate.split(":", 3)
        if len(parts) == 4 and parts[1].isdigit():
            candidate = f"{parts[0]}:{parts[1]}"
            username = parts[2]
            password = parts[3]

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


def parse_proxy_list(
    payload: str,
    *,
    default_scheme: str = "socks5",
) -> list[dict[str, str]]:
    """Parse a newline-delimited proxy pool and remove exact duplicates."""
    lines = [
        line.strip()
        for line in str(payload or "").replace("\r", "\n").split("\n")
        if line.strip()
    ]
    if not lines:
        raise ProxyError("代理池为空，请每行粘贴一个代理")
    proxies: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    errors: list[str] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            proxy = parse_proxy_payload(
                line,
                default_scheme=default_scheme,
            )
        except ProxyError as exc:
            errors.append(f"第 {line_number} 行：{exc}")
            continue
        key = (
            str(proxy.get("server") or ""),
            str(proxy.get("username") or ""),
            str(proxy.get("password") or ""),
        )
        if key not in seen:
            proxies.append(proxy)
            seen.add(key)
    if errors:
        preview = "\n".join(errors[:5])
        suffix = f"\n其余 {len(errors) - 5} 行同样有误" if len(errors) > 5 else ""
        raise ProxyError(f"代理池有 {len(errors)} 行格式错误：\n{preview}{suffix}")
    if not proxies:
        raise ProxyError("代理池没有可用代理")
    return proxies


class ProxyPoolRotator:
    """Thread-safe sequential pool; each node is leased 5–8 times by default."""

    def __init__(
        self,
        config: ProxyConfig,
        *,
        randint: Any = random.randint,
    ):
        self.proxies = parse_proxy_list(
            config.pool,
            default_scheme=config.effective_proxy_type(),
        )
        self.uses_min = min(100, max(1, int(config.pool_uses_min)))
        self.uses_max = min(100, max(1, int(config.pool_uses_max)))
        if self.uses_min > self.uses_max:
            self.uses_min, self.uses_max = self.uses_max, self.uses_min
        self.randint = randint
        self.lock = threading.Lock()
        self.index = 0
        self.use_number = 0
        self.use_limit = int(self.randint(self.uses_min, self.uses_max))

    def next(self) -> ProxyPoolLease:
        with self.lock:
            if self.use_number >= self.use_limit:
                self.index = (self.index + 1) % len(self.proxies)
                self.use_number = 0
                self.use_limit = int(
                    self.randint(self.uses_min, self.uses_max)
                )
            self.use_number += 1
            return ProxyPoolLease(
                proxy=dict(self.proxies[self.index]),
                pool_index=self.index + 1,
                pool_size=len(self.proxies),
                use_number=self.use_number,
                use_limit=self.use_limit,
            )


def fetch_proxy_from_api(
    config: ProxyConfig,
    *,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, str]:
    url = config.api_url.strip()
    if not url.startswith(("https://", "http://")):
        raise ProxyError("代理提取接口需要以 https:// 或 http:// 开头")
    qg_api = is_qg_proxy_api_url(url)
    if qg_api:
        parsed = urlsplit(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        api_key = config.api_key.strip() or str(query.get("key") or "").strip()
        if not api_key:
            raise ProxyError("请填写青果代理 API Key")
        query["key"] = api_key
        # One short-lived QG node belongs to one browser. Override links copied
        # with distinct=false so parallel launches cannot intentionally reuse it.
        query["num"] = "1"
        query["distinct"] = "true"
        url = urlunsplit(parsed._replace(query=urlencode(query)))
    try:
        with httpx.Client(
            timeout=max(3, int(config.api_timeout_seconds)),
            follow_redirects=True,
            trust_env=False,
            transport=transport,
            headers={
                "Accept": "text/plain, application/json",
                "User-Agent": "CTExcelApplyClient/2.5.9",
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
    if qg_api:
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            # format=txt 时成功响应就是 HOST:PORT；继续交给通用解析器。
            payload = None
        if payload is not None:
            if not isinstance(payload, dict):
                raise ProxyError("青果代理接口返回结构错误")
            code = str(payload.get("code") or "").strip()
            request_id = str(payload.get("request_id") or "").strip()
            request_suffix = (
                f"，request_id: {request_id}" if request_id else ""
            )
            if code != "SUCCESS":
                detail = QG_PROXY_ERROR_MESSAGES.get(
                    code,
                    "代理提取失败",
                )
                raise ProxyError(
                    f"{detail}（{code or 'UNKNOWN'}{request_suffix}）"
                )
            if not isinstance(payload.get("data"), list) or not payload["data"]:
                raise ProxyError(
                    "青果代理接口未返回 IP 资源"
                    f"（SUCCESS{request_suffix}）"
                )

    return parse_proxy_payload(
        response.text,
        default_scheme=config.effective_proxy_type(),
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
    if mode == "pool":
        return parse_proxy_list(
            config.pool,
            default_scheme=config.effective_proxy_type(),
        )[0]
    if mode == "tunnel":
        result = config.playwright_proxy()
        if not result:
            raise ProxyError("请填写青果隧道地址、端口、AuthKey 和 AuthPwd")
        if not config.username.strip() or not config.password:
            raise ProxyError("请填写青果隧道 AuthKey 和 AuthPwd")
        tunnel_host = urlsplit(result["server"]).hostname or ""
        if not (
            tunnel_host.lower().startswith("tun-")
            and tunnel_host.lower().endswith(".qg.net")
        ):
            raise ProxyError(
                "青果隧道地址应为 tun-*.qg.net；当前地址不是青果隧道入口"
            )
        return result
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
    while b"\r\n" not in response and len(response) < 16384:
        chunk = sock.recv(512)
        if not chunk:
            break
        response.extend(chunk)
    raw_response = bytes(response)
    first_line = raw_response.split(b"\r\n", 1)[0].decode(
        "latin-1",
        "replace",
    )
    if re.match(r"HTTP/\d(?:\.\d)?\s+200\b", first_line):
        return
    while len(response) < 16384:
        try:
            chunk = sock.recv(512)
        except socket.timeout:
            break
        if not chunk:
            break
        response.extend(chunk)
    body = bytes(response).partition(b"\r\n\r\n")[2]
    detail = " ".join(
        body.decode("utf-8", "replace").split()
    )[:500]
    message = f"HTTP 代理 CONNECT 返回 {first_line}"
    if detail:
        message += f"：{detail}"
    raise ProxyError(message)


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


def _read_socks_address(sock: socket.socket, address_type: int) -> bytes:
    if address_type == 1:
        return _recv_exact(sock, 4)
    if address_type == 3:
        length = _recv_exact(sock, 1)
        return length + _recv_exact(sock, length[0])
    if address_type == 4:
        return _recv_exact(sock, 16)
    raise ProxyError("SOCKS5 请求的地址格式不受支持")


def _connect_authenticated_socks5(
    upstream: socket.socket,
    proxy: dict[str, str],
    address_type: int,
    encoded_address: bytes,
    encoded_port: bytes,
) -> bytes:
    username = str(proxy.get("username") or "")
    password = str(proxy.get("password") or "")
    methods = b"\x00\x02" if username else b"\x00"
    upstream.sendall(b"\x05" + bytes([len(methods)]) + methods)
    version, method = _recv_exact(upstream, 2)
    if version != 5 or method == 0xFF:
        raise ProxyError("SOCKS5 上游代理拒绝认证")
    if method == 2:
        user_bytes = username.encode("utf-8")
        pass_bytes = password.encode("utf-8")
        if not user_bytes or len(user_bytes) > 255 or len(pass_bytes) > 255:
            raise ProxyError("SOCKS5 上游代理账号密码格式错误")
        upstream.sendall(
            b"\x01"
            + bytes([len(user_bytes)])
            + user_bytes
            + bytes([len(pass_bytes)])
            + pass_bytes
        )
        auth_version, auth_status = _recv_exact(upstream, 2)
        if auth_version != 1 or auth_status != 0:
            raise ProxyError("SOCKS5 上游代理账号密码验证失败")
    elif method != 0:
        raise ProxyError("SOCKS5 上游代理认证方式不受支持")

    upstream.sendall(
        b"\x05\x01\x00"
        + bytes([address_type])
        + encoded_address
        + encoded_port
    )
    header = _recv_exact(upstream, 4)
    if header[0] != 5 or header[1] != 0:
        raise ProxyError(
            f"SOCKS5 上游代理连接目标失败（代码 {header[1]}）"
        )
    bound_address = _read_socks_address(upstream, header[3])
    bound_port = _recv_exact(upstream, 2)
    return header + bound_address + bound_port


def _relay_sockets(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    while True:
        readable, _, _ = select.select(sockets, [], [], 30)
        if not readable:
            continue
        for source in readable:
            data = source.recv(65536)
            if not data:
                return
            destination = right if source is left else left
            destination.sendall(data)


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class AuthenticatedSocks5Bridge:
    """Expose an unauthenticated loopback SOCKS5 endpoint for Chromium.

    Chromium/Playwright rejects SOCKS5 username/password fields. The bridge is
    local-only and performs username/password authentication against the real
    upstream node before relaying traffic.
    """

    def __init__(self, upstream_proxy: dict[str, str]):
        parsed = urlsplit(str(upstream_proxy.get("server") or ""))
        if parsed.scheme != "socks5" or not parsed.hostname or not parsed.port:
            raise ProxyError("SOCKS5 本地桥接的上游地址无效")
        self.upstream_proxy = dict(upstream_proxy)
        self.host = parsed.hostname
        self.port = parsed.port
        bridge = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                client = self.request
                client.settimeout(30)
                try:
                    version, method_count = _recv_exact(client, 2)
                    if version != 5:
                        return
                    _recv_exact(client, method_count)
                    client.sendall(b"\x05\x00")
                    version, command, _reserved, address_type = _recv_exact(
                        client,
                        4,
                    )
                    if version != 5 or command != 1:
                        client.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                        return
                    address = _read_socks_address(client, address_type)
                    port = _recv_exact(client, 2)
                    with socket.create_connection(
                        (bridge.host, bridge.port),
                        timeout=15,
                    ) as upstream:
                        upstream.settimeout(30)
                        reply = _connect_authenticated_socks5(
                            upstream,
                            bridge.upstream_proxy,
                            address_type,
                            address,
                            port,
                        )
                        client.sendall(reply)
                        client.settimeout(None)
                        upstream.settimeout(None)
                        _relay_sockets(client, upstream)
                except (OSError, ProxyError):
                    with contextlib.suppress(OSError):
                        client.sendall(
                            b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00"
                        )

        self.server = _ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="ctexcel-socks5-bridge",
            daemon=True,
        )
        self.thread.start()

    @property
    def playwright_proxy(self) -> dict[str, str]:
        port = int(self.server.server_address[1])
        return {"server": f"socks5://127.0.0.1:{port}"}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@dataclass
class BrowserProxyRoute:
    proxy: Optional[dict[str, str]]
    bridge: Optional[AuthenticatedSocks5Bridge] = None

    def close(self) -> None:
        if self.bridge:
            self.bridge.close()
            self.bridge = None


def browser_compatible_proxy(
    proxy: Optional[dict[str, str]],
) -> BrowserProxyRoute:
    if not proxy:
        return BrowserProxyRoute(proxy=None)
    parsed = urlsplit(str(proxy.get("server") or ""))
    if parsed.scheme == "socks5" and proxy.get("username"):
        bridge = AuthenticatedSocks5Bridge(proxy)
        return BrowserProxyRoute(
            proxy=bridge.playwright_proxy,
            bridge=bridge,
        )
    return BrowserProxyRoute(proxy=dict(proxy))


def prepare_proxy(
    config: ProxyConfig,
    *,
    resolved_proxy: Optional[dict[str, str]] = None,
    probe_tunnel: bool = False,
) -> PreparedProxy:
    """提取、检测公网 IP，并在创建客户前验证代理。"""
    api_mode = config.mode.strip().lower() == "api"
    qg_api = api_mode and is_qg_proxy_api_url(config.api_url)
    public_ip = ""
    public_ip_error = ""
    if api_mode and not qg_api:
        try:
            public_ip = detect_public_ip()
        except ProxyError as exc:
            public_ip_error = str(exc)

    try:
        playwright_proxy = (
            dict(resolved_proxy)
            if resolved_proxy is not None
            else resolve_proxy(config)
        )
        tunnel_mode = config.mode.strip().lower() == "tunnel"
        if playwright_proxy and (not tunnel_mode or probe_tunnel):
            probe_proxy_endpoint(playwright_proxy)
    except ProxyError as exc:
        details = [str(exc)]
        if qg_api:
            details.extend(
                [
                    "",
                    "提取接口的 key 只负责获取节点；代理连接另行使用 "
                    "Authkey / Authpwd。",
                    "请在 API 动态提取模式填写代理连接账号密码，并确认"
                    "代理协议与所购产品一致。",
                ]
            )
        elif api_mode:
            details.extend(
                [
                    "",
                    (
                        f"当前出口公网 IP：{public_ip}"
                        if public_ip
                        else f"当前出口公网 IP：{public_ip_error or '检测失败'}"
                    ),
                    "请确认代理平台白名单填写的是上面的公网 IP，"
                    "而不是局域网 IP 或服务器 IP。",
                ]
            )
            if config.effective_proxy_type() == "socks5":
                details.append("当前提取结果已按 SOCKS5 协议验证。")
        raise ProxyError("\n".join(details)) from exc

    return PreparedProxy(
        playwright_proxy=playwright_proxy,
        public_ip=public_ip,
        public_ip_error=public_ip_error,
    )


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
