from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch
import time

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
        "api_version": 8,
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


def test_client_customer_request_key_is_idempotent(client_api):
    client, db_path = client_api
    email_mock = AsyncMock(
        return_value={
            "email": "parallel@example.test",
            "email_account_id": "parallel-mail",
            "email_provider_id": None,
            "email_provider_domain": None,
            "share_link": "",
            "is_email_auto": True,
        }
    )
    payload = {
        "reuse_pending": False,
        "allow_new_after_checkpoint": True,
        "request_key": "batch_parallel_1234567890",
    }
    with patch.object(
        main,
        "_generate_email_account",
        new=email_mock,
    ), patch.object(
        main,
        "regenerate_identity",
        new=AsyncMock(),
    ):
        first = client.post(
            "/api/ctexcel-client/customers",
            headers=AUTH_HEADERS,
            json=payload,
        )
        replay = client.post(
            "/api/ctexcel-client/customers",
            headers=AUTH_HEADERS,
            json=payload,
        )

    assert first.status_code == 201, first.text
    assert replay.status_code == 201, replay.text
    assert replay.json()["customer_id"] == first.json()["customer_id"]
    assert replay.json()["idempotent_replay"] is True
    email_mock.assert_awaited_once()
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """SELECT id, ctexcel_client_request_key
               FROM customers WHERE product_type = 'ctexcel'"""
        ).fetchall()
    assert rows == [
        (
            first.json()["customer_id"],
            "batch_parallel_1234567890",
        )
    ]


def test_concurrent_same_request_key_creates_one_customer(client_api):
    client, db_path = client_api
    calls = []

    async def generate_email(**_kwargs):
        calls.append("generate")
        await asyncio.sleep(0.15)
        return {
            "email": "concurrent-same-key@example.test",
            "email_account_id": "concurrent-same-key-mail",
            "email_provider_id": None,
            "email_provider_domain": None,
            "share_link": "",
            "is_email_auto": True,
        }

    payload = {
        "reuse_pending": False,
        "request_key": "same_key_parallel_1234567890",
    }
    with patch.object(
        main,
        "_generate_email_account",
        new=generate_email,
    ), patch.object(main, "regenerate_identity", new=AsyncMock()):
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(
                executor.map(
                    lambda _index: client.post(
                        "/api/ctexcel-client/customers",
                        headers=AUTH_HEADERS,
                        json=payload,
                    ),
                    range(2),
                )
            )

    assert [response.status_code for response in responses] == [201, 201]
    bodies = [response.json() for response in responses]
    assert len({body["customer_id"] for body in bodies}) == 1
    assert sum(bool(body.get("idempotent_replay")) for body in bodies) == 1
    assert calls == ["generate"]
    with sqlite3.connect(db_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM customers WHERE product_type = 'ctexcel'"
        ).fetchone()[0]
    assert count == 1


def test_concurrent_request_keys_cannot_claim_same_pending_customer(client_api):
    client, db_path = client_api
    with sqlite3.connect(db_path) as connection:
        customer_id = connection.execute(
            """INSERT INTO customers (product_type, email, activation_date)
               VALUES ('ctexcel', 'claim-once@example.test', '2026-08-01')"""
        ).lastrowid
        connection.commit()

    def claim(key):
        return client.post(
            "/api/ctexcel-client/customers",
            headers=AUTH_HEADERS,
            json={
                "reuse_pending": True,
                "resume_customer_id": customer_id,
                "request_key": key,
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                claim,
                (
                    "claim_key_alpha_1234567890",
                    "claim_key_bravo_1234567890",
                ),
            )
        )

    assert sorted(response.status_code for response in responses) == [201, 409]
    winner = next(response for response in responses if response.status_code == 201)
    assert winner.json()["customer_id"] == customer_id
    with sqlite3.connect(db_path) as connection:
        owner, locked_at = connection.execute(
            """SELECT automation_lock_owner, automation_locked_at
               FROM customers WHERE id = ?""",
            (customer_id,),
        ).fetchone()
    assert owner in {
        "claim_key_alpha_1234567890",
        "claim_key_bravo_1234567890",
    }
    assert locked_at


def test_only_claim_owner_can_release_an_unfinished_customer(client_api):
    client, db_path = client_api
    request_key = "release_owner_key_1234567890"
    with sqlite3.connect(db_path) as connection:
        customer_id = connection.execute(
            """INSERT INTO customers (product_type, email, activation_date)
               VALUES ('ctexcel', 'release@example.test', '2026-08-01')"""
        ).lastrowid
        connection.commit()

    claimed = client.post(
        "/api/ctexcel-client/customers",
        headers=AUTH_HEADERS,
        json={
            "reuse_pending": True,
            "resume_customer_id": customer_id,
            "request_key": request_key,
        },
    )
    assert claimed.status_code == 201, claimed.text

    wrong_owner = client.post(
        f"/api/ctexcel-client/customers/{customer_id}/release",
        headers=AUTH_HEADERS,
        json={"request_key": "release_wrong_key_1234567890"},
    )
    assert wrong_owner.status_code == 200
    assert wrong_owner.json()["released"] is False

    released = client.post(
        f"/api/ctexcel-client/customers/{customer_id}/release",
        headers=AUTH_HEADERS,
        json={"request_key": request_key},
    )
    assert released.status_code == 200
    assert released.json()["released"] is True
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            """SELECT automation_lock_owner, automation_locked_at
               FROM customers WHERE id = ?""",
            (customer_id,),
        ).fetchone() == (None, None)

    pending = client.get(
        "/api/ctexcel-client/customers/pending",
        headers=AUTH_HEADERS,
    )
    assert [item["customer_id"] for item in pending.json()["customers"]] == [
        customer_id
    ]


def test_verification_provider_http_does_not_block_event_loop(client_api):
    _client, _db_path = client_api

    class SlowProvider:
        def get_email_messages(self, _account_id):
            time.sleep(0.2)
            return {
                "messages": [
                    {
                        "id": "message-1",
                        "subject": "Confirm it's you: 123456",
                        "text": "Your code is 123456",
                        "receivedAt": "2026-08-01T00:00:00Z",
                    }
                ]
            }

    async def scenario():
        with patch.object(
            main,
            "get_customer",
            new=AsyncMock(
                return_value={
                    "id": 1,
                    "email": "async@example.test",
                    "moemail_address": "async@example.test",
                }
            ),
        ), patch.object(
            main,
            "_resolve_inbox_provider",
            new=AsyncMock(return_value=("mailbox-1", SlowProvider())),
        ):
            started = time.monotonic()
            task = asyncio.create_task(main.get_customer_verification_code(1))
            await asyncio.sleep(0.02)
            responsive_after = time.monotonic() - started
            result = await task
        return responsive_after, result

    responsive_after, result = asyncio.run(scenario())
    assert responsive_after < 0.1
    assert result.code == "123456"


def test_backup_roundtrip_preserves_ctexcel_completion_state(client_api):
    client, db_path = client_api
    with sqlite3.connect(db_path) as connection:
        customer_id = connection.execute(
            """INSERT INTO customers
               (product_type, email, activation_date,
                ctexcel_registration_confirmed_at,
                ctexcel_payment_succeeded_at,
                ctexcel_login_account, ctexcel_initial_password)
               VALUES ('ctexcel', 'completed@example.test', '2026-08-01',
                       '2026-08-01T01:00:00Z', '2026-08-01T01:05:00Z',
                       '447900000789', 'Backup*Pass1')"""
        ).lastrowid
        connection.commit()

    payload = asyncio.run(main._export_backup_payload())
    assert payload["customers"][0]["ctexcel_login_account"] == "447900000789"
    assert payload["customers"][0]["ctexcel_initial_password"] == "Backup*Pass1"
    restored = asyncio.run(main._restore_backup_payload(payload))

    assert restored["customers_restored"] == 1
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """SELECT ctexcel_registration_confirmed_at,
                      ctexcel_payment_succeeded_at,
                      ctexcel_login_account, ctexcel_initial_password
               FROM customers WHERE id = ?""",
            (customer_id,),
        ).fetchone()
    assert row == (
        "2026-08-01T01:00:00Z",
        "2026-08-01T01:05:00Z",
        "447900000789",
        "Backup*Pass1",
    )
    pending = client.get(
        "/api/ctexcel-client/customers/pending",
        headers=AUTH_HEADERS,
    )
    assert pending.status_code == 200
    assert pending.json()["customers"] == []


def test_client_reuses_order_checkpoint_without_payment_success(client_api):
    client, db_path = client_api
    with sqlite3.connect(db_path) as connection:
        unpaid_pending_id = connection.execute(
            """INSERT INTO customers
               (product_type, email, activation_date, ctexcel_order_number,
                ctexcel_transaction_amount)
               VALUES ('ctexcel', 'unpaid-pending@example.test', '2026-07-31',
                       'ORDERSUK2026073104095817734376', '1.00')"""
        ).lastrowid
        connection.commit()

    response = client.post(
        "/api/ctexcel-client/customers",
        headers=AUTH_HEADERS,
        json={
            "reuse_pending": True,
            "allow_new_after_checkpoint": True,
            "resume_customer_id": unpaid_pending_id,
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["reused"] is True
    assert response.json()["customer_id"] == unpaid_pending_id
    assert response.json()["email"] == "unpaid-pending@example.test"


def test_client_does_not_reuse_confirmed_payment_customer(client_api):
    client, db_path = client_api
    with sqlite3.connect(db_path) as connection:
        paid_customer_id = connection.execute(
            """INSERT INTO customers
               (product_type, email, activation_date, ctexcel_order_number,
                ctexcel_transaction_amount, ctexcel_payment_succeeded_at)
               VALUES ('ctexcel', 'paid@example.test', '2026-07-31',
                       'ORDERSUK2026073104095817734376', '1.00',
                       '2026-07-31T07:00:00Z')"""
        ).lastrowid
        connection.commit()

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
    assert response.json()["customer_id"] != paid_customer_id
    assert response.json()["email"] == "next-batch@example.test"


def test_client_does_not_reuse_customer_with_confirmation_email(client_api):
    client, db_path = client_api
    with sqlite3.connect(db_path) as connection:
        confirmed_id = connection.execute(
            """INSERT INTO customers
               (product_type, email, activation_date,
                ctexcel_registration_confirmed_at)
               VALUES ('ctexcel', 'confirmed@example.test', '2026-07-31',
                       '2026-07-31T07:05:00Z')"""
        ).lastrowid
        connection.commit()

    email_mock = AsyncMock(
        return_value={
            "email": "next-registration@example.test",
            "email_account_id": "next-registration-mail",
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
            json={"reuse_pending": True},
        )

    assert response.status_code == 201, response.text
    assert response.json()["reused"] is False
    assert response.json()["customer_id"] != confirmed_id
    assert response.json()["email"] == "next-registration@example.test"

    pending = client.get(
        "/api/ctexcel-client/customers/pending",
        headers=AUTH_HEADERS,
    ).json()["customers"]
    assert all(
        item["customer_id"] != confirmed_id
        for item in pending
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


def test_client_payment_checkpoint_persists_success_page_fields(client_api):
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
            "phone_number": "447900000009",
            "payment_succeeded": True,
        },
    )

    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.json()
    assert payload["ok"] is True
    assert payload["customer_id"] == ctexcel_id
    assert payload["order_number"] == "ORDERSUK2026073104095817734376"
    assert payload["transaction_amount"] == "1.00"
    assert payload["phone_number"] == "447900000009"
    assert payload["payment_succeeded"] is True
    assert payload["payment_succeeded_at"]
    with sqlite3.connect(db_path) as connection:
        persisted = connection.execute(
            """SELECT ctexcel_order_number, ctexcel_transaction_amount,
                      phone_number, ctexcel_payment_succeeded_at,
                      public_version
               FROM customers WHERE id = ?""",
            (ctexcel_id,),
        ).fetchone()
    assert persisted == (
        "ORDERSUK2026073104095817734376",
        "1.00",
        "447900000009",
        payload["payment_succeeded_at"],
        2,
    )

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
    assert client.post(
        f"/api/ctexcel-client/customers/{ctexcel_id}/payment-checkpoint",
        headers=AUTH_HEADERS,
        json={
            "transaction_amount": "1.00",
            "phone_number": "invalid-phone",
        },
    ).status_code == 400


def test_payment_checkpoint_refreshes_active_customer_lease(client_api):
    client, db_path = client_api
    with sqlite3.connect(db_path) as connection:
        customer_id = connection.execute(
            """INSERT INTO customers
               (product_type, email, activation_date,
                automation_lock_owner, automation_locked_at)
               VALUES ('ctexcel', 'lease-refresh@example.test', '2026-08-01',
                       'lease_refresh_key_1234567890',
                       '2026-08-01T00:00:00Z')"""
        ).lastrowid
        connection.commit()

    response = client.post(
        f"/api/ctexcel-client/customers/{customer_id}/payment-checkpoint",
        headers=AUTH_HEADERS,
        json={
            "order_number": "ORDER202608010000000001",
            "transaction_amount": "1.00",
        },
    )

    assert response.status_code == 200, response.text
    with sqlite3.connect(db_path) as connection:
        owner, locked_at = connection.execute(
            """SELECT automation_lock_owner, automation_locked_at
               FROM customers WHERE id = ?""",
            (customer_id,),
        ).fetchone()
    assert owner == "lease_refresh_key_1234567890"
    assert locked_at != "2026-08-01T00:00:00Z"


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
