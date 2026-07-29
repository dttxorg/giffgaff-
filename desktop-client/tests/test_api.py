from __future__ import annotations

import json

import httpx

from ctexcel_client.api import AdminApi, ApiError


SECRET_PATH = "/" + ("a" * 40)


def test_hidden_entry_login_and_customer_creation_flow():
    state = {"authenticated": False}
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        path = request.url.path
        cookie = request.headers.get("cookie", "")
        if path == SECRET_PATH:
            return httpx.Response(
                302,
                headers={
                    "location": "/index.html",
                    "set-cookie": (
                        "__Host-giffgaff_admin_entry=entry-cookie;"
                        " Path=/; Secure; HttpOnly; SameSite=Lax"
                    ),
                },
            )
        if path == "/index.html":
            assert "__Host-giffgaff_admin_entry=entry-cookie" in cookie
            return httpx.Response(200, text="<html>login</html>")
        if path == "/api/auth/status":
            return httpx.Response(
                200,
                json={
                    "auth_required": True,
                    "authenticated": state["authenticated"],
                },
            )
        if path == "/api/auth/login":
            assert "__Host-giffgaff_admin_entry=entry-cookie" in cookie
            assert json.loads(request.content) == {"password": "app-secret"}
            state["authenticated"] = True
            return httpx.Response(
                200,
                json={"ok": True},
                headers={
                    "set-cookie": (
                        "__Host-giffgaff_label_auth=auth-cookie;"
                        " Path=/; Secure; HttpOnly; SameSite=Lax"
                    )
                },
            )
        if path == "/api/customers":
            assert request.method == "POST"
            assert "__Host-giffgaff_admin_entry=entry-cookie" in cookie
            assert "__Host-giffgaff_label_auth=auth-cookie" in cookie
            body = json.loads(request.content)
            assert body["product_type"] == "ctexcel"
            assert body["email"] == ""
            assert body["use_sim_code"] is False
            assert body["shipping_address"] == "fixed shipping address"
            return httpx.Response(
                201,
                json={
                    "customer_id": 321,
                    "product_type": "ctexcel",
                    "email": "customer@example.test",
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")

    with AdminApi(
        "https://manager.example.test",
        SECRET_PATH,
        "app-secret",
        transport=httpx.MockTransport(handler),
    ) as api:
        status = api.connect()
        created = api.create_ctexcel_customer("fixed shipping address")

    assert status["authenticated"] is True
    assert created == {
        "customer_id": 321,
        "product_type": "ctexcel",
        "email": "customer@example.test",
    }
    assert requests[:5] == [
        ("GET", SECRET_PATH),
        ("GET", "/index.html"),
        ("GET", "/api/auth/status"),
        ("POST", "/api/auth/login"),
        ("GET", "/api/auth/status"),
    ]
    assert requests[-1] == ("POST", "/api/customers")


def test_hidden_entry_404_has_actionable_message():
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(404, text="Not found")
    )

    with AdminApi(
        "https://manager.example.test",
        SECRET_PATH,
        "app-secret",
        transport=transport,
    ) as api:
        try:
            api.connect()
        except ApiError as exc:
            message = str(exc)
        else:
            raise AssertionError("expected ApiError")

    assert "隐藏入口返回 404" in message
