"""隐藏管理入口回归测试。

公开二维码路由不经过入口门禁；浏览器管理页面/API 必须先通过随机入口，
之后仍需原有 APP_PASSWORD 登录。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

import crud
import database
import main


SECRET_PATH = "/entry_7LzQ0vF4mN9kR2xC8pT6wY3sH1jB5dGa"
PUBLIC_TOKEN = "public-token-12345678901234567890"


@pytest.fixture
def hidden_admin_client():
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "test.db")
        original = {
            "database_path": database.DATABASE_PATH,
            "crud_path": crud.DATABASE_PATH,
            "main_path": main.DATABASE_PATH,
            "app_password": main.APP_PASSWORD,
            "admin_entry_path": main.ADMIN_ENTRY_PATH,
        }
        database.DATABASE_PATH = db_path
        crud.DATABASE_PATH = db_path
        main.DATABASE_PATH = db_path
        main.APP_PASSWORD = "test-password-that-remains-required"
        main.ADMIN_ENTRY_PATH = SECRET_PATH
        main._reset_login_failure_state()
        asyncio.run(database.init_db())

        async def _seed_public_customer():
            import aiosqlite
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    """INSERT INTO customers (email, activation_date, public_token)
                       VALUES (?, ?, ?)""",
                    ("public@example.com", "2026-07-15", PUBLIC_TOKEN),
                )
                await db.commit()

        asyncio.run(_seed_public_customer())
        client = TestClient(main.app, base_url="https://testserver")
        try:
            yield client
        finally:
            client.close()
            main._reset_login_failure_state()
            database.DATABASE_PATH = original["database_path"]
            crud.DATABASE_PATH = original["crud_path"]
            main.DATABASE_PATH = original["main_path"]
            main.APP_PASSWORD = original["app_password"]
            main.ADMIN_ENTRY_PATH = original["admin_entry_path"]


@pytest.mark.parametrize("path", [
    "/",
    "/index.html",
    "/worker_setup.js",
    "/api/customers",
    "/api/agent/ping",
    "/api/auth/status",
])
def test_management_surfaces_are_uniform_404_without_entry_cookie(hidden_admin_client, path):
    response = hidden_admin_client.get(path)

    assert response.status_code == 404
    assert response.text == "Not found"
    assert "giffgaff" not in response.text.lower()
    assert "登录" not in response.text
    assert response.headers["Cache-Control"] == "no-store, max-age=0"


def test_password_login_endpoint_is_also_hidden_without_entry_cookie(hidden_admin_client):
    response = hidden_admin_client.post(
        "/api/auth/login",
        json={"password": "test-password-that-remains-required"},
    )

    assert response.status_code == 404
    assert response.text == "Not found"


def test_frontend_turns_expired_entry_plaintext_into_a_clear_reentry_prompt():
    html = (BACKEND_DIR.parent / "frontend" / "index.html").read_text(encoding="utf-8")

    assert 'id="entry-expired-screen"' in html
    assert "window.fetch = async function guardedFetch" in html
    assert "rawBody.trim() !== 'Not found'" in html
    assert "管理入口已过期，请重新访问隐藏管理入口" in html
    assert "if (!res.ok)" in html[html.index("async function checkAuth"):]


@pytest.mark.parametrize("path", [
    "/",
    "/index.html",
    "//index.html",
    "/INDEX.HTML",
    "/worker_setup.js",
    "///worker_setup.js",
    "/WORKER_SETUP.JS",
])
def test_management_frontend_cache_path_normalization(path):
    assert main._is_management_frontend_path(path)


def test_frontend_revision_refreshes_when_index_changes(tmp_path, monkeypatch):
    index = tmp_path / "index.html"
    index.write_text("first admin bundle", encoding="utf-8")
    monkeypatch.setattr(main, "FRONTEND_DIR", str(tmp_path))
    monkeypatch.setattr(main, "_FRONTEND_REVISION_STATE", None)

    first = main._frontend_revision()
    first_stat = index.stat()
    index.write_text("other admin bundle", encoding="utf-8")
    os.utime(
        index,
        ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns),
    )
    second = main._frontend_revision()

    assert first == hashlib.sha256(b"first admin bundle").hexdigest()[:12]
    assert index.stat().st_size == first_stat.st_size
    assert index.stat().st_mtime_ns == first_stat.st_mtime_ns
    assert second == hashlib.sha256(b"other admin bundle").hexdigest()[:12]
    assert second != first


def test_secret_entry_sets_secure_signed_cookie_then_shows_password_login(hidden_admin_client):
    entry = hidden_admin_client.get(SECRET_PATH, follow_redirects=False)
    expected_revision = hashlib.sha256(
        (BACKEND_DIR.parent / "frontend" / "index.html").read_bytes()
    ).hexdigest()[:12]
    expected_index_url = f"/index.html?v={expected_revision}"

    assert entry.status_code == 302
    assert entry.headers["location"] == expected_index_url
    assert entry.headers["Cache-Control"] == "no-store, max-age=0"
    assert entry.headers["Pragma"] == "no-cache"
    assert entry.headers["Expires"] == "Thu, 01 Jan 1970 00:00:00 GMT"
    set_cookie = entry.headers["set-cookie"]
    assert main.ADMIN_ENTRY_COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie
    assert f"Max-Age={main.ADMIN_ENTRY_TTL_SECONDS}" in set_cookie
    assert "expires=" in set_cookie.lower()
    assert "Domain=" not in set_cookie
    assert main.ADMIN_ENTRY_COOKIE_NAME.startswith("__Host-")
    cookie_value = hidden_admin_client.cookies.get(main.ADMIN_ENTRY_COOKIE_NAME)
    assert cookie_value not in (None, "", "true")
    version, issued_at, nonce, signature = cookie_value.split(".")
    assert version == "v1"
    assert abs(int(issued_at) - int(time.time())) <= 5
    assert len(nonce) >= 32
    assert len(signature) == 64

    page = hidden_admin_client.get("/index.html")
    assert page.status_code == 200
    assert "访问口令" in page.text
    assert page.headers["Cache-Control"] == "no-store, max-age=0"
    assert page.headers["Pragma"] == "no-cache"
    assert page.headers["Expires"] == "Thu, 01 Jan 1970 00:00:00 GMT"

    versioned_page = hidden_admin_client.get(entry.headers["location"])
    assert versioned_page.status_code == 200
    assert versioned_page.text == page.text

    revalidated_page = hidden_admin_client.get(
        "/index.html",
        headers={"If-None-Match": page.headers["ETag"]},
    )
    assert revalidated_page.status_code == 304
    assert revalidated_page.headers["Cache-Control"] == "no-store, max-age=0"
    assert revalidated_page.headers["Pragma"] == "no-cache"
    assert revalidated_page.headers["Expires"] == "Thu, 01 Jan 1970 00:00:00 GMT"

    root = hidden_admin_client.get("/", follow_redirects=False)
    assert root.status_code == 307
    assert root.headers["location"] == expected_index_url
    assert root.headers["Cache-Control"] == "no-store, max-age=0"

    worker_setup = hidden_admin_client.get("/worker_setup.js")
    assert worker_setup.status_code == 200
    assert worker_setup.headers["Cache-Control"] == "no-store, max-age=0"
    assert worker_setup.headers["Pragma"] == "no-cache"
    assert worker_setup.headers["Expires"] == "Thu, 01 Jan 1970 00:00:00 GMT"

    # 隐藏入口只解除 404 门禁，不替代原有密码认证。
    before_login = hidden_admin_client.get("/api/customers")
    assert before_login.status_code == 401
    login = hidden_admin_client.post(
        "/api/auth/login",
        json={"password": "test-password-that-remains-required"},
    )
    assert login.status_code == 200
    auth_set_cookie = login.headers["set-cookie"]
    assert main.AUTH_COOKIE_NAME.startswith("__Host-")
    assert main.AUTH_COOKIE_NAME in auth_set_cookie
    assert "HttpOnly" in auth_set_cookie
    assert "Secure" in auth_set_cookie
    assert "SameSite=lax" in auth_set_cookie
    assert "Path=/" in auth_set_cookie
    assert "Domain=" not in auth_set_cookie
    assert hidden_admin_client.get("/api/customers").status_code == 200
    assert hidden_admin_client.get("/api/agent/ping").status_code == 404


def test_public_qr_routes_work_without_admin_entry_cookie(hidden_admin_client):
    version = hidden_admin_client.get(f"/api/public/{PUBLIC_TOKEN}/version")
    page = hidden_admin_client.get(f"/p/{PUBLIC_TOKEN}")

    assert version.status_code == 200
    assert version.json() == {"public_version": 3_000_001}
    assert page.status_code == 200
    assert "public@example.com" in page.text


@pytest.mark.parametrize("forged", [
    "true",
    "forged-payload.invalid-signature",
    "A" * 43 + "." + "0" * 64,
    "v1.1700000000." + "A" * 43 + "." + "0" * 64,
])
def test_forged_admin_entry_cookie_is_rejected(hidden_admin_client, forged):
    response = hidden_admin_client.get(
        "/index.html",
        headers={"Cookie": f"{main.ADMIN_ENTRY_COOKIE_NAME}={forged}"},
    )

    assert response.status_code == 404
    assert response.text == "Not found"


def test_tampering_with_a_real_cookie_invalidates_it(hidden_admin_client):
    hidden_admin_client.get(SECRET_PATH, follow_redirects=False)
    real_cookie = hidden_admin_client.cookies.get(main.ADMIN_ENTRY_COOKIE_NAME)
    replacement = "0" if real_cookie[-1] != "0" else "1"
    tampered = real_cookie[:-1] + replacement

    # 显式 Cookie header 覆盖 cookie jar，验证真实签名被改一位后也不能通过。
    hidden_admin_client.cookies.clear()
    response = hidden_admin_client.get(
        "/index.html",
        headers={"Cookie": f"{main.ADMIN_ENTRY_COOKIE_NAME}={tampered}"},
    )

    assert response.status_code == 404


def test_admin_entry_cookie_expires_after_twelve_hours(hidden_admin_client):
    now = int(time.time())
    still_valid = main._new_admin_entry_cookie(
        SECRET_PATH,
        issued_at=now - main.ADMIN_ENTRY_TTL_SECONDS + 60,
    )
    expired = main._new_admin_entry_cookie(
        SECRET_PATH,
        issued_at=now - main.ADMIN_ENTRY_TTL_SECONDS - 1,
    )

    valid_response = hidden_admin_client.get(
        "/index.html",
        headers={"Cookie": f"{main.ADMIN_ENTRY_COOKIE_NAME}={still_valid}"},
    )
    expired_response = hidden_admin_client.get(
        "/index.html",
        headers={"Cookie": f"{main.ADMIN_ENTRY_COOKIE_NAME}={expired}"},
    )

    assert valid_response.status_code == 200
    assert expired_response.status_code == 404


def test_login_failures_are_rate_limited_per_ip_and_success_clears_them(hidden_admin_client):
    hidden_admin_client.get(SECRET_PATH, follow_redirects=False)
    ip_header = {"CF-Connecting-IP": "203.0.113.10"}

    for _ in range(main.LOGIN_FAILURE_LIMIT):
        response = hidden_admin_client.post(
            "/api/auth/login",
            json={"password": "wrong-password"},
            headers=ip_header,
        )
        assert response.status_code == 401

    limited = hidden_admin_client.post(
        "/api/auth/login",
        json={"password": "wrong-password"},
        headers=ip_header,
    )
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["Retry-After"]) <= main.LOGIN_FAILURE_WINDOW_SECONDS

    # 限制按 IP 隔离，另一个地址仍有自己的失败额度。
    other_ip = hidden_admin_client.post(
        "/api/auth/login",
        json={"password": "wrong-password"},
        headers={"CF-Connecting-IP": "203.0.113.11"},
    )
    assert other_ip.status_code == 401

    # 正确密码仍可验证，并清除该 IP 的失败记录。
    success = hidden_admin_client.post(
        "/api/auth/login",
        json={"password": "test-password-that-remains-required"},
        headers=ip_header,
    )
    assert success.status_code == 200
    after_success = hidden_admin_client.post(
        "/api/auth/login",
        json={"password": "wrong-password"},
        headers=ip_header,
    )
    assert after_success.status_code == 401


def test_login_failure_window_expires_after_ten_minutes():
    client_ip = "198.51.100.25"
    main._reset_login_failure_state()
    try:
        for offset in range(main.LOGIN_FAILURE_LIMIT):
            allowed, _ = main._register_login_failure(client_ip, now=1000 + offset)
            assert allowed is True
        allowed, retry_after = main._register_login_failure(client_ip, now=1005)
        assert allowed is False
        assert retry_after > 0

        allowed_after_window, retry_after = main._register_login_failure(
            client_ip,
            now=1000 + main.LOGIN_FAILURE_WINDOW_SECONDS + 1,
        )
        assert allowed_after_window is True
        assert retry_after == 0
    finally:
        main._reset_login_failure_state()


def test_logout_deletes_host_auth_cookie_with_matching_security_attributes(hidden_admin_client):
    hidden_admin_client.get(SECRET_PATH, follow_redirects=False)
    hidden_admin_client.post(
        "/api/auth/login",
        json={"password": "test-password-that-remains-required"},
    )

    logout = hidden_admin_client.post("/api/auth/logout")

    assert logout.status_code == 200
    set_cookie = logout.headers["set-cookie"]
    assert main.AUTH_COOKIE_NAME in set_cookie
    assert "Max-Age=0" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie
    assert "Domain=" not in set_cookie


@pytest.mark.parametrize("weak_path", [
    "/admin",
    "/secret",
    "/too/short",
    "missing-leading-slash-12345678901234567890",
])
def test_weak_or_malformed_admin_entry_paths_are_rejected(weak_path):
    original = main.ADMIN_ENTRY_PATH
    main.ADMIN_ENTRY_PATH = weak_path
    try:
        with pytest.raises(ValueError):
            main._validated_admin_entry_path()
    finally:
        main.ADMIN_ENTRY_PATH = original
