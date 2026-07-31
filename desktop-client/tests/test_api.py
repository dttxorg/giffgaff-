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
                    "api_version": 8,
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
                            "registration_confirmed_at": None,
                        }
                    ],
                },
            )
        if path == "/api/ctexcel-client/customers":
            assert request.method == "POST"
            assert json.loads(request.content) == {
                "reuse_pending": True,
                "allow_new_after_checkpoint": False,
            }
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
        if path == "/api/ctexcel-client/customers/321/payment-checkpoint":
            assert request.method == "POST"
            assert json.loads(request.content) == {
                "order_number": "ORDERSUK2026073104095817734376",
                "transaction_amount": "1.00",
                "payment_succeeded": True,
            }
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "customer_id": 321,
                    "order_number": "ORDERSUK2026073104095817734376",
                    "transaction_amount": "1.00",
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
        checkpoint = api.save_payment_checkpoint(
            created["customer_id"],
            order_number="ORDERSUK2026073104095817734376",
            transaction_amount="1.00",
            payment_succeeded=True,
        )
        order_info = api.sync_order_info(created["customer_id"])

    assert status["ctexcel_customer_count"] == 3
    assert status["pending_customer_count"] == 1
    assert status["api_version"] == 8
    assert pending[0]["customer_id"] == 321
    assert created["email"] == "customer@example.test"
    assert verification["code"] == "123456"
    assert checkpoint["transaction_amount"] == "1.00"
    assert order_info["phone_number"] == "07900000009"
    assert requests == [
        ("GET", "/api/ctexcel-client/status"),
        ("GET", "/api/ctexcel-client/customers/pending"),
        ("POST", "/api/ctexcel-client/customers"),
        ("GET", "/api/ctexcel-client/customers/321/verification-code"),
        ("POST", "/api/ctexcel-client/customers/321/payment-checkpoint"),
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


def test_client_api_retries_cloudflare_5xx_before_creating_customer():
    attempts = []
    messages = []
    sleeps = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(json.loads(request.content))
        if len(attempts) < 3:
            return httpx.Response(
                502,
                text=(
                    "The origin web server returned an invalid or "
                    "incomplete response to Cloudflare."
                ),
            )
        return httpx.Response(
            201,
            json={
                "customer_id": 489,
                "email": "retry@example.test",
                "reused": True,
            },
        )

    with AdminApi(
        "https://manager.example.test",
        "app-secret",
        transport=httpx.MockTransport(handler),
        retry_callback=messages.append,
        retry_delays=(2, 4),
        sleep=sleeps.append,
    ) as api:
        created = api.create_ctexcel_customer(
            allow_new_after_checkpoint=True,
            resume_customer_id=488,
            request_key="batch_retry_1234567890",
        )

    assert created["customer_id"] == 489
    assert len(attempts) == 3
    assert attempts == [
        {
            "reuse_pending": True,
            "allow_new_after_checkpoint": True,
            "resume_customer_id": 488,
            "request_key": "batch_retry_1234567890",
        }
    ] * 3
    assert sleeps == [2.0, 4.0]
    assert "HTTP 502" in messages[0]
    assert "自动重试 1/2" in messages[0]
    assert "自动重试 2/2" in messages[1]


def test_client_api_retries_incomplete_success_response():
    responses = iter(
        [
            httpx.Response(200, text="<html>temporary edge page</html>"),
            httpx.Response(
                200,
                json={
                    "ok": True,
                    "api_version": 8,
                    "ctexcel_customer_count": 10,
                    "pending_customer_count": 2,
                },
            ),
        ]
    )
    messages = []

    with AdminApi(
        "https://manager.example.test",
        "app-secret",
        transport=httpx.MockTransport(lambda _request: next(responses)),
        retry_callback=messages.append,
        retry_delays=(0,),
        sleep=lambda _delay: None,
    ) as api:
        status = api.connect()

    assert status["ok"] is True
    assert "响应格式不完整" in messages[0]
