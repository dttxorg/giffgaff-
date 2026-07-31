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
        "api_version": 4,
        "ctexcel_customer_count": 0,
        "pending_customer_count": 0,
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

    email_mock = AsyncMock(return_value=email_bundle)
    with patch.object(
        main,
        "_generate_email_account",
        new=email_mock,
    ), patch.object(main, "regenerate_identity", new=identity_mock):
        response = client.post(
            "/api/ctexcel-client/customers",
            headers=AUTH_HEADERS,
            json={"shipping_address": "fixed shipping address"},
        )
        reused_response = client.post(
            "/api/ctexcel-client/customers",
            headers=AUTH_HEADERS,
            json={"shipping_address": "another fixed address"},
        )

    assert response.status_code == 201, response.text
    data = response.json()
    assert data["product_type"] == "ctexcel"
    assert data["email"] == "new-client@example.test"
    assert data["sim_activation_code"] is None
    assert data["reused"] is False
    assert reused_response.status_code == 201
    assert reused_response.json()["customer_id"] == data["customer_id"]
    assert reused_response.json()["reused"] is True
    email_mock.assert_awaited_once()
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
        None,
        "client-mail-account",
        None,
        None,
        None,
    )

    pending = client.get(
        "/api/ctexcel-client/customers/pending",
        headers=AUTH_HEADERS,
    )
    assert pending.status_code == 200
    assert pending.json()["count"] == 1
    assert pending.json()["customers"][0]["customer_id"] == data["customer_id"]


def test_continuous_client_can_create_after_paid_pending_customer(client_api):
    client, db_path = client_api
    with sqlite3.connect(db_path) as connection:
        paid_pending_id = connection.execute(
            """INSERT INTO customers
               (product_type, email, activation_date, ctexcel_order_number,
                ctexcel_transaction_amount)
               VALUES ('ctexcel', 'paid-pending@example.test', '2026-07-31',
                       'ORDERSUK2026073104095817734376', '1.00')"""
        ).lastrowid
        connection.commit()

    blocked = client.post(
        "/api/ctexcel-client/customers",
        headers=AUTH_HEADERS,
        json={"reuse_pending": True},
    )
    assert blocked.status_code == 409

    email_mock = AsyncMock(
        return_value={
            "email": "next-batch@example.test",
            "email_account_id": "next-batch-mail",
            "email_provider_id": None,
            "email_provider_domain": None,
            "share_link": "",
            "is_email_auto": True,
        }
    )
    with patch.object(
        main,
        "_generate_email_account",
        new=email_mock,
    ), patch.object(
        main,
        "regenerate_identity",
        new=AsyncMock(),
    ):
        response = client.post(
            "/api/ctexcel-client/customers",
            headers=AUTH_HEADERS,
            json={
                "reuse_pending": True,
                "allow_new_after_checkpoint": True,
            },
        )

    assert response.status_code == 201, response.text
    assert response.json()["reused"] is False
    assert response.json()["customer_id"] != paid_pending_id
    assert response.json()["email"] == "next-batch@example.test"


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


def test_client_payment_checkpoint_persists_order_and_amount(client_api):
    client, db_path = client_api
    with sqlite3.connect(db_path) as connection:
        ctexcel_id = connection.execute(
            """INSERT INTO customers
               (product_type, email, activation_date)
               VALUES ('ctexcel', 'payment@example.test', '2026-07-31')"""
        ).lastrowid
        giffgaff_id = connection.execute(
            """INSERT INTO customers
               (product_type, email, activation_date)
               VALUES ('giffgaff', 'other@example.test', '2026-07-31')"""
        ).lastrowid
        connection.commit()

    response = client.post(
        f"/api/ctexcel-client/customers/{ctexcel_id}/payment-checkpoint",
        headers=AUTH_HEADERS,
        json={
            "order_number": "ORDERSUK2026073104095817734376",
            "transaction_amount": "1",
        },
    )

    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "ok": True,
        "customer_id": ctexcel_id,
        "order_number": "ORDERSUK2026073104095817734376",
        "transaction_amount": "1.00",
    }
    with sqlite3.connect(db_path) as connection:
        persisted = connection.execute(
            """SELECT ctexcel_order_number, ctexcel_transaction_amount
               FROM customers WHERE id = ?""",
            (ctexcel_id,),
        ).fetchone()
    assert persisted == ("ORDERSUK2026073104095817734376", "1.00")

    assert client.post(
        f"/api/ctexcel-client/customers/{giffgaff_id}/payment-checkpoint",
        headers=AUTH_HEADERS,
        json={"transaction_amount": "1.00"},
    ).status_code == 400
    assert client.post(
        f"/api/ctexcel-client/customers/{ctexcel_id}/payment-checkpoint",
        headers=AUTH_HEADERS,
        json={"transaction_amount": "not-money"},
    ).status_code == 400


def test_client_order_sync_endpoint_is_scoped_and_returns_phone(client_api):
    client, db_path = client_api
    with sqlite3.connect(db_path) as connection:
        ctexcel_id = connection.execute(
            """INSERT INTO customers
               (product_type, email, activation_date, email_account_id)
               VALUES ('ctexcel', 'order@example.test', '2026-07-29', 'mail-1')"""
        ).lastrowid
        giffgaff_id = connection.execute(
            """INSERT INTO customers
               (product_type, email, activation_date, email_account_id)
               VALUES ('giffgaff', 'other@example.test', '2026-07-29', 'mail-2')"""
        ).lastrowid
        connection.commit()

    order_info = main.CTExcelOrderInfoOut(
        found=True,
        phone_number="07900000009",
        order_number="ORDER2026072912345678901",
        checked_count=1,
        detail="已同步",
    )
    sync_mock = AsyncMock(return_value=order_info)
    with patch.object(main, "_sync_ctexcel_order_info", new=sync_mock):
        response = client.post(
            f"/api/ctexcel-client/customers/{ctexcel_id}/order-info",
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert response.json()["phone_number"] == "07900000009"
    sync_mock.assert_awaited_once()
    assert client.post(
        f"/api/ctexcel-client/customers/{giffgaff_id}/order-info",
        headers=AUTH_HEADERS,
    ).status_code == 400
