from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

import crud
import database
import main


CLIENT_PASSWORD = "ctexcel-client-test-password"
ENTRY_PATH = "/entry_7LzQ0vF4mN9kR2xC8pT6wY3sH1jB5dGa"
AUTH_HEADERS = {"Authorization": f"Bearer {CLIENT_PASSWORD}"}


@pytest.fixture
def client_api():
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "test.db")
        original = (
            database.DATABASE_PATH,
            crud.DATABASE_PATH,
            main.DATABASE_PATH,
            main.APP_PASSWORD,
            main.ADMIN_ENTRY_PATH,
        )
        database.DATABASE_PATH = db_path
        crud.DATABASE_PATH = db_path
        main.DATABASE_PATH = db_path
        main.APP_PASSWORD = CLIENT_PASSWORD
        main.ADMIN_ENTRY_PATH = ENTRY_PATH
        main._reset_login_failure_state()
        asyncio.run(database.init_db())
        test_client = TestClient(main.app, base_url="https://testserver")
        try:
            yield test_client, db_path
        finally:
            test_client.close()
            main._reset_login_failure_state()
            (
                database.DATABASE_PATH,
                crud.DATABASE_PATH,
                main.DATABASE_PATH,
                main.APP_PASSWORD,
                main.ADMIN_ENTRY_PATH,
            ) = original


def test_client_status_uses_bearer_password_without_hidden_entry_cookie(client_api):
    client, _ = client_api

    assert client.get("/api/ctexcel-client/status").status_code == 401
    assert client.get(
        "/api/ctexcel-client/status",
        headers={"Authorization": "Bearer wrong"},
    ).status_code == 401

    response = client.get("/api/ctexcel-client/status", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "api_version": 1,
        "ctexcel_customer_count": 0,
    }
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get("/api/customers", headers=AUTH_HEADERS).status_code == 404


def test_client_api_rate_limits_bad_password_and_recovers_on_success(client_api):
    client, _ = client_api

    statuses = [
        client.get(
            "/api/ctexcel-client/status",
            headers={"Authorization": "Bearer wrong"},
        ).status_code
        for _ in range(6)
    ]

    assert statuses == [401, 401, 401, 401, 401, 429]
    assert client.get(
        "/api/ctexcel-client/status",
        headers=AUTH_HEADERS,
    ).status_code == 200


def test_client_api_creates_ctexcel_customer_and_dedicated_email(client_api):
    client, db_path = client_api
    email_bundle = {
        "email": "new-client@example.test",
        "email_account_id": "client-mail-account",
        "email_provider_id": None,
        "email_provider_domain": None,
        "share_link": "",
        "is_email_auto": True,
    }
    identity_mock = AsyncMock()

    with patch.object(
        main,
        "_generate_email_account",
        new=AsyncMock(return_value=email_bundle),
    ), patch.object(main, "regenerate_identity", new=identity_mock):
        response = client.post(
            "/api/ctexcel-client/customers",
            headers=AUTH_HEADERS,
            json={"shipping_address": "fixed shipping address"},
        )

    assert response.status_code == 201, response.text
    data = response.json()
    assert data["product_type"] == "ctexcel"
    assert data["email"] == "new-client@example.test"
    assert data["sim_activation_code"] is None
    identity_mock.assert_not_awaited()
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """SELECT product_type, email, shipping_address, email_account_id,
                      sim_code_id, first_name, address
               FROM customers WHERE id = ?""",
            (data["customer_id"],),
        ).fetchone()
    assert row == (
        "ctexcel",
        "new-client@example.test",
        "fixed shipping address",
        "client-mail-account",
        None,
        None,
        None,
    )


def test_client_verification_endpoint_is_scoped_to_ctexcel(client_api):
    client, db_path = client_api
    with sqlite3.connect(db_path) as connection:
        ctexcel_id = connection.execute(
            """INSERT INTO customers
               (product_type, email, activation_date, email_account_id)
               VALUES ('ctexcel', 'code@example.test', '2026-07-29', 'mail-1')"""
        ).lastrowid
        giffgaff_id = connection.execute(
            """INSERT INTO customers
               (product_type, email, activation_date, email_account_id)
               VALUES ('giffgaff', 'other@example.test', '2026-07-29', 'mail-2')"""
        ).lastrowid
        connection.commit()

    verification = main.VerificationCodeOut(
        found=True,
        code="123456",
        email="code@example.test",
        checked_count=1,
        detail="已提取最新验证码",
    )
    with patch.object(
        main,
        "get_customer_verification_code",
        new=AsyncMock(return_value=verification),
    ):
        response = client.get(
            f"/api/ctexcel-client/customers/{ctexcel_id}/verification-code",
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert response.json()["code"] == "123456"
    assert client.get(
        f"/api/ctexcel-client/customers/{giffgaff_id}/verification-code",
        headers=AUTH_HEADERS,
    ).status_code == 400
