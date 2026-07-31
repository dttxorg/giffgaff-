from __future__ import annotations

import httpx
import pytest

from ctexcel_client.config import DEFAULT_PROXY_API_URL, ProxyConfig
from ctexcel_client.proxy import (
    ProxyError,
    ProxyPoolRotator,
    browser_compatible_proxy,
    detect_public_ip,
    masked_proxy_label,
    parse_proxy_list,
    parse_proxy_payload,
    prepare_proxy,
    probe_proxy_endpoint,
    resolve_proxy,
)


LEGACY_CLIPROXY_API_URL = (
    "https://api.cliproxy.io/white/api"
    "?region=Rand&num=1&time=10&format=n&type=txt"
)


def test_plain_text_proxy_api_defaults_to_socks5():
    proxy = parse_proxy_payload("203.0.113.10:1080\n")

    assert proxy == {"server": "socks5://203.0.113.10:1080"}
    assert masked_proxy_label(proxy) == "socks5://203.0.*.*:1080"


def test_proxy_parser_accepts_credentials_and_json_payloads():
    authenticated = parse_proxy_payload(
        "203.0.113.11:1080:proxy-user:proxy-pass"
    )
    json_proxy = parse_proxy_payload(
        '{"data": [{"ip": "203.0.113.12", "port": 8080}]}',
        default_scheme="http",
    )
    json_server = parse_proxy_payload(
        '{"server": "socks5://203.0.113.13:1080"}'
    )
    hostname_credentials = parse_proxy_payload(
        "proxy.example.test:3010:proxy-user:proxy-password"
    )

    assert authenticated == {
        "server": "socks5://203.0.113.11:1080",
        "username": "proxy-user",
        "password": "proxy-pass",
    }
    assert json_proxy == {"server": "http://203.0.113.12:8080"}
    assert json_server == {"server": "socks5://203.0.113.13:1080"}
    assert hostname_credentials == {
        "server": "socks5://proxy.example.test:3010",
        "username": "proxy-user",
        "password": "proxy-password",
    }


def test_proxy_pool_parses_many_lines_deduplicates_and_rotates():
    payload = "\n".join(
        (
            "proxy-a.example.test:3010:user-a:pass-a",
            "proxy-a.example.test:3010:user-a:pass-a",
            "proxy-b.example.test:3011:user-b:pass-b",
            "proxy-c.example.test:3012:user-c:pass-c",
        )
    )
    parsed = parse_proxy_list(payload)
    assert len(parsed) == 3

    rotator = ProxyPoolRotator(
        ProxyConfig(
            mode="pool",
            pool=payload,
            pool_uses_min=5,
            pool_uses_max=8,
        ),
        randint=lambda _minimum, _maximum: 5,
    )
    leases = [rotator.next() for _ in range(11)]

    assert [lease.pool_index for lease in leases] == [
        1,
        1,
        1,
        1,
        1,
        2,
        2,
        2,
        2,
        2,
        3,
    ]
    assert leases[0].use_number == 1
    assert leases[4].use_number == 5
    assert leases[5].use_number == 1


def test_authenticated_socks5_is_bridged_for_chromium_without_credentials():
    route = browser_compatible_proxy(
        {
            "server": "socks5://proxy.example.test:3010",
            "username": "proxy-user",
            "password": "proxy-password",
        }
    )
    try:
        assert route.bridge is not None
        assert route.proxy is not None
        assert route.proxy["server"].startswith("socks5://127.0.0.1:")
        assert "username" not in route.proxy
        assert "password" not in route.proxy
    finally:
        route.close()


def test_proxy_pool_reports_the_bad_line_without_echoing_other_credentials():
    with pytest.raises(ProxyError, match="第 2 行") as exc_info:
        parse_proxy_list(
            "proxy.example.test:3010:user:secret\nnot-a-proxy"
        )

    assert "secret" not in str(exc_info.value)


def test_dynamic_proxy_api_is_resolved_for_each_request():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="203.0.113.20:2080")

    config = ProxyConfig(
        mode="api",
        proxy_type="socks5",
        api_url="https://proxy.example.test/extract?num=1",
    )
    first = resolve_proxy(config, transport=httpx.MockTransport(handler))
    second = resolve_proxy(config, transport=httpx.MockTransport(handler))

    assert first == {"server": "socks5://203.0.113.20:2080"}
    assert second == first
    assert len(calls) == 2


def test_cliproxy_whitelist_api_is_always_treated_as_socks5():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="proxy.example.test:3010:proxy-user:proxy-password",
        )

    config = ProxyConfig(
        mode="api",
        proxy_type="http",
        api_url=LEGACY_CLIPROXY_API_URL,
    )

    assert resolve_proxy(
        config,
        transport=httpx.MockTransport(handler),
    ) == {
        "server": "socks5://proxy.example.test:3010",
        "username": "proxy-user",
        "password": "proxy-password",
    }


def test_qg_proxy_api_injects_key_and_uses_data_server():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "code": "SUCCESS",
                "data": [
                    {
                        "proxy_ip": "198.51.100.24",
                        "server": "proxy-node.example.test:59419",
                        "area": "测试省测试市",
                        "isp": "电信",
                        "deadline": "2026-07-31 15:38:36",
                    }
                ],
                "request_id": "request-success-1",
            },
        )

    config = ProxyConfig(
        mode="api",
        proxy_type="http",
        api_url=(
            "https://share.proxy.qg.net/get"
            "?area=350500%2C330700&isp=1&distinct=true"
        ),
        api_key="secret-qg-key",
    )

    proxy = resolve_proxy(config, transport=httpx.MockTransport(handler))

    assert proxy == {"server": "http://proxy-node.example.test:59419"}
    assert len(calls) == 1
    assert calls[0].url.params["key"] == "secret-qg-key"
    assert calls[0].url.params["num"] == "1"
    assert calls[0].url.params["area"] == "350500,330700"


def test_qg_proxy_api_accepts_txt_format_and_preserves_parameters():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text="proxy-txt.example.test:59419\r\n")

    config = ProxyConfig(
        mode="api",
        proxy_type="http",
        api_url=(
            "https://share.proxy.qg.net/get"
            "?num=1&area=360000&isp=0&format=txt"
            "&seq=%5Cr%5Cn&distinct=false"
        ),
        api_key="sample-qg-key",
    )

    proxy = resolve_proxy(config, transport=httpx.MockTransport(handler))

    assert proxy == {"server": "http://proxy-txt.example.test:59419"}
    assert len(calls) == 1
    assert calls[0].url.params["key"] == "sample-qg-key"
    assert calls[0].url.params["num"] == "1"
    assert calls[0].url.params["area"] == "360000"
    assert calls[0].url.params["isp"] == "0"
    assert calls[0].url.params["format"] == "txt"
    assert calls[0].url.params["seq"] == r"\r\n"
    assert calls[0].url.params["distinct"] == "true"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("REQUEST_LIMIT_EXCEEDED", "60 次/分钟"),
        ("EXTRACT_LIMIT_EXCEEDED", "今日 IP 提取配额已用完"),
        ("INVALID_KEY", "API Key 不存在或已过期"),
    ],
)
def test_qg_proxy_api_maps_error_code_and_request_id(code, expected):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": code, "data": [], "request_id": "request-error-1"},
        )

    config = ProxyConfig(
        mode="api",
        api_url=DEFAULT_PROXY_API_URL,
        api_key="secret-qg-key",
    )

    with pytest.raises(ProxyError) as exc_info:
        resolve_proxy(config, transport=httpx.MockTransport(handler))

    message = str(exc_info.value)
    assert expected in message
    assert code in message
    assert "request-error-1" in message
    assert "secret-qg-key" not in message


def test_qg_proxy_api_requires_key_before_request():
    config = ProxyConfig(mode="api", api_url=DEFAULT_PROXY_API_URL)

    with pytest.raises(ProxyError, match="请填写青果代理 API Key"):
        resolve_proxy(config)


def test_public_ip_detection_accepts_json_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.ipify.org"
        return httpx.Response(200, json={"ip": "8.8.4.4"})

    assert detect_public_ip(
        transport=httpx.MockTransport(handler)
    ) == "8.8.4.4"


def test_public_ip_detection_falls_back_to_plain_text():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.ipify.org":
            return httpx.Response(503)
        return httpx.Response(200, text="1.1.1.1\n")

    assert detect_public_ip(
        transport=httpx.MockTransport(handler)
    ) == "1.1.1.1"


def test_fixed_socks5_proxy_uses_optional_credentials():
    config = ProxyConfig(
        mode="custom",
        proxy_type="socks5",
        host="203.0.113.30",
        port="1080",
        username="user",
        password="pass",
    )

    assert resolve_proxy(config) == {
        "server": "socks5://203.0.113.30:1080",
        "username": "user",
        "password": "pass",
    }


def test_qg_tunnel_uses_fixed_http_gateway_and_credentials():
    config = ProxyConfig(
        mode="tunnel",
        proxy_type="socks5",
        host="tun-example.qg.net",
        port="14600",
        username="auth-key",
        password="auth-password",
    )

    assert resolve_proxy(config) == {
        "server": "http://tun-example.qg.net:14600",
        "username": "auth-key",
        "password": "auth-password",
    }
    assert config.effective_proxy_type() == "http"


def test_qg_tunnel_requires_connection_credentials():
    config = ProxyConfig(
        mode="tunnel",
        host="tun-example.qg.net",
        port="14600",
    )

    with pytest.raises(ProxyError, match="AuthKey 和 AuthPwd"):
        resolve_proxy(config)


def test_qg_tunnel_prepare_skips_separate_connect_probe(monkeypatch):
    config = ProxyConfig(
        mode="tunnel",
        host="tun-example.qg.net",
        port="14600",
        username="auth-key",
        password="auth-password",
    )

    def unexpected_probe(_proxy):
        raise AssertionError("tunnel should be loaded directly by browser")

    monkeypatch.setattr(
        "ctexcel_client.proxy.probe_proxy_endpoint",
        unexpected_probe,
    )

    prepared = prepare_proxy(config)

    assert prepared.playwright_proxy == {
        "server": "http://tun-example.qg.net:14600",
        "username": "auth-key",
        "password": "auth-password",
    }


class FakeSocket:
    def __init__(self, responses: list[bytes]):
        self.responses = list(responses)
        self.sent: list[bytes] = []
        self.timeout = 0.0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def settimeout(self, timeout: float):
        self.timeout = timeout

    def sendall(self, payload: bytes):
        self.sent.append(payload)

    def recv(self, size: int) -> bytes:
        if not self.responses:
            return b""
        payload = self.responses.pop(0)
        assert len(payload) <= size
        return payload


def test_socks5_probe_performs_handshake_and_connects_to_ctexcel(monkeypatch):
    fake_socket = FakeSocket(
        [
            b"\x05\x00",
            b"\x05\x00\x00\x01",
            b"\x7f\x00\x00\x01",
            b"\x1f\x90",
        ]
    )
    monkeypatch.setattr(
        "ctexcel_client.proxy.socket.create_connection",
        lambda *_args, **_kwargs: fake_socket,
    )

    probe_proxy_endpoint({"server": "socks5://203.0.113.40:1080"})

    assert fake_socket.timeout == 8
    assert fake_socket.sent[0] == b"\x05\x01\x00"
    assert b"www.ctexcel.com" in fake_socket.sent[1]


def test_socks5_probe_supports_username_password(monkeypatch):
    fake_socket = FakeSocket(
        [
            b"\x05\x02",
            b"\x01\x00",
            b"\x05\x00\x00\x03",
            b"\x09",
            b"localhost",
            b"\x1f\x90",
        ]
    )
    monkeypatch.setattr(
        "ctexcel_client.proxy.socket.create_connection",
        lambda *_args, **_kwargs: fake_socket,
    )

    probe_proxy_endpoint(
        {
            "server": "socks5://203.0.113.41:1080",
            "username": "proxy-user",
            "password": "proxy-pass",
        }
    )

    assert fake_socket.sent[0] == b"\x05\x02\x00\x02"
    assert b"proxy-user" in fake_socket.sent[1]
    assert b"proxy-pass" in fake_socket.sent[1]


def test_socks5_probe_reports_whitelist_or_auth_rejection(monkeypatch):
    fake_socket = FakeSocket([b"\x05\xff"])
    monkeypatch.setattr(
        "ctexcel_client.proxy.socket.create_connection",
        lambda *_args, **_kwargs: fake_socket,
    )

    with pytest.raises(ProxyError, match="公网 IP 白名单"):
        probe_proxy_endpoint({"server": "socks5://203.0.113.42:1080"})


def test_http_proxy_probe_uses_connect_and_optional_auth(monkeypatch):
    fake_socket = FakeSocket(
        [b"HTTP/1.1 200 Connection established\r\n\r\n"]
    )
    monkeypatch.setattr(
        "ctexcel_client.proxy.socket.create_connection",
        lambda *_args, **_kwargs: fake_socket,
    )

    probe_proxy_endpoint(
        {
            "server": "http://203.0.113.43:8080",
            "username": "proxy-user",
            "password": "proxy-pass",
        }
    )

    request = fake_socket.sent[0].decode("ascii")
    assert request.startswith(
        "CONNECT www.ctexcel.com:443 HTTP/1.1\r\n"
    )
    assert "Proxy-Authorization: Basic " in request
    assert "proxy-pass" not in request


def test_http_proxy_probe_surfaces_actual_407_detail(monkeypatch):
    fake_socket = FakeSocket(
        [
            b"HTTP/1.1 407 Proxy Authentication Required\r\n"
            b"Proxy-Authenticate: Basic realm=\"\"\r\n\r\n",
            b"proxy authorization invalid, client ip 192.0.2.10 "
            b"authorization failed",
        ]
    )
    monkeypatch.setattr(
        "ctexcel_client.proxy.socket.create_connection",
        lambda *_args, **_kwargs: fake_socket,
    )

    with pytest.raises(ProxyError) as exc_info:
        probe_proxy_endpoint({"server": "http://203.0.113.43:8080"})

    message = str(exc_info.value)
    assert "407 Proxy Authentication Required" in message
    assert "client ip 192.0.2.10 authorization failed" in message


def test_proxy_api_http_error_does_not_echo_secret_url():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    secret = "private-api-token"
    config = ProxyConfig(
        mode="api",
        api_url=f"https://proxy.example.test/extract?token={secret}",
    )

    with pytest.raises(ProxyError) as exc_info:
        resolve_proxy(config, transport=httpx.MockTransport(handler))

    assert "HTTP 403" in str(exc_info.value)
    assert secret not in str(exc_info.value)


def test_prepare_proxy_adds_detected_public_ip_to_whitelist_error(
    monkeypatch,
):
    config = ProxyConfig(mode="api", api_url=LEGACY_CLIPROXY_API_URL)
    monkeypatch.setattr(
        "ctexcel_client.proxy.detect_public_ip",
        lambda: "8.8.8.8",
    )
    monkeypatch.setattr(
        "ctexcel_client.proxy.resolve_proxy",
        lambda _config: {"server": "socks5://proxy.example.test:3010"},
    )

    def reject(_proxy):
        raise ProxyError("SOCKS5 代理拒绝认证")

    monkeypatch.setattr(
        "ctexcel_client.proxy.probe_proxy_endpoint",
        reject,
    )

    with pytest.raises(ProxyError) as exc_info:
        prepare_proxy(config)

    message = str(exc_info.value)
    assert "当前出口公网 IP：8.8.8.8" in message
    assert "局域网 IP 或服务器 IP" in message
    assert "已按 SOCKS5 协议验证" in message


def test_qg_prepare_proxy_explains_separate_connection_credentials(
    monkeypatch,
):
    config = ProxyConfig(
        mode="api",
        proxy_type="socks5",
        api_url=DEFAULT_PROXY_API_URL,
        api_key="sample-key",
    )

    def unexpected_public_ip_detection():
        raise AssertionError("QG links do not need public IP detection")

    monkeypatch.setattr(
        "ctexcel_client.proxy.detect_public_ip",
        unexpected_public_ip_detection,
    )
    monkeypatch.setattr(
        "ctexcel_client.proxy.resolve_proxy",
        lambda _config: {"server": "socks5://proxy.example.test:3010"},
    )

    def reject(_proxy):
        raise ProxyError("代理服务器连接或协议握手失败")

    monkeypatch.setattr(
        "ctexcel_client.proxy.probe_proxy_endpoint",
        reject,
    )

    with pytest.raises(ProxyError) as exc_info:
        prepare_proxy(config)

    message = str(exc_info.value)
    assert "提取接口的 key 只负责获取节点" in message
    assert "Authkey / Authpwd" in message
    assert "当前出口公网 IP" not in message
    assert "局域网 IP 或服务器 IP" not in message


def test_qg_api_uses_http_and_connection_credentials():
    config = ProxyConfig(
        mode="api",
        proxy_type="socks5",
        api_url=DEFAULT_PROXY_API_URL,
        username="auth-key",
        password="auth-password",
    )

    assert config.effective_proxy_type() == "http"
