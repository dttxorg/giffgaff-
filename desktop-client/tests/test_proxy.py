from __future__ import annotations

import httpx
import pytest

from ctexcel_client.config import ProxyConfig
from ctexcel_client.proxy import (
    ProxyError,
    masked_proxy_label,
    parse_proxy_payload,
    probe_proxy_endpoint,
    resolve_proxy,
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

    assert authenticated == {
        "server": "socks5://203.0.113.11:1080",
        "username": "proxy-user",
        "password": "proxy-pass",
    }
    assert json_proxy == {"server": "http://203.0.113.12:8080"}
    assert json_server == {"server": "socks5://203.0.113.13:1080"}


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
    fake_socket = FakeSocket([b"HTTP/1.1 200 Connection established\r\n"])
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
