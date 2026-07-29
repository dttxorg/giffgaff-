from __future__ import annotations

import json

import httpx

from ctexcel_client.api import AdminApi, ApiError


def test_scoped_api_connection_customer_creation_and_verification_flow():
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer app-secret"
        path = request.url.path
        if path == "/api/ctexcel-client/status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "api_version": 2,
                    "ctexcel_customer_count": 3,
                    "pending_customer_count": 1,
                },
            )
        if path == "/api/ctexcel-client/customers/pending":
            return httpx.Response(
                200,
                json={
                    "count": 1,
                    "customers": [
                        {
                            "customer_id": 321,
                            "email": "customer@example.test",
                            "phone_number": None,
                            "order_number": None,
                        }
                    ],
                },
            )
        if path == "/api/ctexcel-client/customers":
            assert request.method == "POST"
            assert json.loads(request.content) == {"reuse_pending": True}
            return httpx.Response(
                201,
                json={
                    "customer_id": 321,
                    "product_type": "ctexcel",
                    "email": "customer@example.test",
                    "reused": True,
                },
            )
        if path == "/api/ctexcel-client/customers/321/verification-code":
            return httpx.Response(
                200,
                json={
                    "found": True,
                    "code": "123456",
                    "email": "customer@example.test",
                    "checked_count": 1,
                    "detail": "已提取最新验证码",
                },
            )
        if path == "/api/ctexcel-client/customers/321/order-info":
            assert request.method == "POST"
            return httpx.Response(
                200,
                json={
                    "found": True,
                    "phone_number": "07900000009",
                    "order_number": "ORDER2026072912345678901",
                    "checked_count": 1,
                    "detail": "已同步",
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")

    with AdminApi(
        "https://manager.example.test",
        "app-secret",
        transport=httpx.MockTransport(handler),
    ) as api:
        status = api.connect()
        pending = api.pending_customers()
        created = api.create_ctexcel_customer()
        verification = api.verification_code(created["customer_id"])
        order_info = api.sync_order_info(created["customer_id"])

    assert status["ctexcel_customer_count"] == 3
    assert status["pending_customer_count"] == 1
    assert pending[0]["customer_id"] == 321
    assert created["email"] == "customer@example.test"
    assert verification["code"] == "123456"
    assert order_info["phone_number"] == "07900000009"
    assert requests == [
        ("GET", "/api/ctexcel-client/status"),
        ("GET", "/api/ctexcel-client/customers/pending"),
        ("POST", "/api/ctexcel-client/customers"),
        ("GET", "/api/ctexcel-client/customers/321/verification-code"),
        ("POST", "/api/ctexcel-client/customers/321/order-info"),
    ]


def test_client_api_reports_wrong_password_clearly():
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            401,
            json={"detail": "客户端连接口令错误"},
        )
    )

    with AdminApi(
        "https://manager.example.test",
        "wrong",
        transport=transport,
    ) as api:
        try:
            api.connect()
        except ApiError as exc:
            message = str(exc)
        else:
            raise AssertionError("expected ApiError")

    assert message == "客户端连接口令错误"


def test_client_api_reports_missing_server_endpoint():
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(404, text="Not found")
    )

    with AdminApi(
        "https://manager.example.test",
        "app-secret",
        transport=transport,
    ) as api:
        try:
            api.connect()
        except ApiError as exc:
            message = str(exc)
        else:
            raise AssertionError("expected ApiError")

    assert message == "服务器尚未启用 CTExcel 客户端 API"
