from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

import crud
import database
import main
from models import CustomerCreate, CustomerUpdate


@pytest.fixture
def ctexcel_client():
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "ctexcel.db")
        original_paths = (database.DATABASE_PATH, crud.DATABASE_PATH, main.DATABASE_PATH)
        original_password = main.APP_PASSWORD
        database.DATABASE_PATH = db_path
        crud.DATABASE_PATH = db_path
        main.DATABASE_PATH = db_path
        main.APP_PASSWORD = ""
        asyncio.run(database.init_db())
        yield TestClient(main.app), db_path
        database.DATABASE_PATH, crud.DATABASE_PATH, main.DATABASE_PATH = original_paths
        main.APP_PASSWORD = original_password


def _insert_ctexcel_customer(db_path: str) -> int:
    with sqlite3.connect(db_path) as connection:
        cursor = connection.execute(
            """INSERT INTO customers
               (product_type, email, activation_date, email_provider_id,
                email_account_id, moemail_id, moemail_address, is_moemail_auto,
                public_token)
               VALUES ('ctexcel', ?, ?, ?, ?, ?, ?, 1, ?)""",
            (
                "ctexcel@example.com",
                "2026-07-25",
                7,
                "mail-account-1",
                "mail-account-1",
                "ctexcel@example.com",
                "ctexcel-public-token",
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def test_app_mode_setting_is_persistent(ctexcel_client):
    client, _ = ctexcel_client

    before = client.get("/api/settings")
    assert before.status_code == 200
    assert before.json()["app_mode"] == "giffgaff"

    saved = client.patch("/api/settings", json={"app_mode": "ctexcel"})
    assert saved.status_code == 200
    assert client.get("/api/settings").json()["app_mode"] == "ctexcel"


def test_nullable_phone_migration_preserves_ctexcel_columns():
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "legacy-not-null.db")
        original_path = database.DATABASE_PATH
        database.DATABASE_PATH = db_path
        try:
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    """CREATE TABLE customers (
                           id INTEGER PRIMARY KEY AUTOINCREMENT,
                           phone_number TEXT NOT NULL UNIQUE,
                           email TEXT NOT NULL,
                           activation_date TEXT NOT NULL,
                           product_type TEXT NOT NULL DEFAULT 'giffgaff',
                           ctexcel_order_number TEXT,
                           created_at TEXT NOT NULL DEFAULT (datetime('now'))
                       )"""
                )
                connection.execute(
                    """INSERT INTO customers
                       (phone_number, email, activation_date, product_type, ctexcel_order_number)
                       VALUES ('07942946765', 'legacy@example.com', '2026-07-25',
                               'ctexcel', 'ORDER-LEGACY-1')"""
                )
                connection.commit()

            asyncio.run(database.init_db())

            with sqlite3.connect(db_path) as connection:
                columns = {
                    row[1]: row for row in connection.execute("PRAGMA table_info(customers)")
                }
                row = connection.execute(
                    """SELECT product_type, phone_number, ctexcel_order_number
                       FROM customers"""
                ).fetchone()
                indexes = {
                    item[1] for item in connection.execute("PRAGMA index_list(customers)")
                }
            assert columns["phone_number"][3] == 0
            assert row == ("ctexcel", "07942946765", "ORDER-LEGACY-1")
            assert "ix_customers_public_token" in indexes
        finally:
            database.DATABASE_PATH = original_path


def test_ctexcel_customer_ignores_sim_and_skips_identity(ctexcel_client):
    client, db_path = ctexcel_client
    with sqlite3.connect(db_path) as connection:
        connection.execute("INSERT INTO sim_codes (code) VALUES ('SIM123')")
        connection.commit()

    email_bundle = {
        "email": "new-ctexcel@example.com",
        "email_account_id": "account-new",
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
            "/api/customers",
            json={
                "product_type": "ctexcel",
                "activation_date": "2026-07-25",
                "use_sim_code": True,
            },
        )

    assert response.status_code == 201, response.text
    assert response.json()["product_type"] == "ctexcel"
    assert response.json()["sim_activation_code"] is None
    identity_mock.assert_not_awaited()
    with sqlite3.connect(db_path) as connection:
        customer = connection.execute(
            """SELECT product_type, sim_code_id, sim_activation_code,
                      first_name, address, postcode
               FROM customers WHERE id = ?""",
            (response.json()["customer_id"],),
        ).fetchone()
        sim = connection.execute(
            "SELECT status, customer_id FROM sim_codes WHERE code = 'SIM123'"
        ).fetchone()
    assert customer == ("ctexcel", None, None, None, None, None)
    assert sim == ("未分配", None)


@pytest.mark.parametrize("provider_kind", ["moemail", "cloudmail"])
def test_ctexcel_order_email_syncs_for_both_provider_routes(ctexcel_client, provider_kind):
    client, db_path = ctexcel_client
    customer_id = _insert_ctexcel_customer(db_path)
    provider = MagicMock(name=f"{provider_kind}-provider")
    provider.get_email_messages.return_value = {
        "messages": [
            {
                "id": f"{provider_kind}-order",
                "subject": "CTExcel 产品订购成功",
                "fromAddress": "service@ctexcel.example",
                "receivedAt": 1784918151251,
            }
        ]
    }
    order_text = """尊敬的用户，您好！
您已成功订购我们的CTExcel产品，订单详情如下：
**订单号：ORDER2026072512362267544904**
**交易金额：£ 178.8**
**手机号码：** **07942946765**
您的专属推荐码：**NTKWJX**
您的专属推荐链接：[https://www.ctexcel.com/uk/buyCard/buyCardPackage/1?recommendCode=NTKWJX](https://www.ctexcel.com/uk/buyCard/buyCardPackage/1?recommendCode=NTKWJX)
"""
    provider.get_message.return_value = {
        "message": (
            {"htmlBody": "<p>" + order_text.replace("\n", "<br>") + "</p>"}
            if provider_kind == "cloudmail"
            else {"text": order_text}
        )
    }

    with patch.object(
        main,
        "_resolve_inbox_provider",
        new=AsyncMock(return_value=("mail-account-1", provider)),
    ):
        response = client.get(f"/api/customers/{customer_id}/ctexcel-order-info")

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["found"] is True
    assert data["phone_number"] == "07942946765"
    assert data["order_number"] == "ORDER2026072512362267544904"
    assert data["transaction_amount"] == "178.8"
    assert data["referral_code"] == "NTKWJX"
    assert data["referral_link"].endswith("recommendCode=NTKWJX")
    assert data["received_at"] == "2026-07-24T18:35:51.251Z"
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """SELECT phone_number, ctexcel_order_number,
                      ctexcel_transaction_amount, ctexcel_referral_code,
                      ctexcel_referral_link, ctexcel_last_checked_at
               FROM customers WHERE id = ?""",
            (customer_id,),
        ).fetchone()
    assert row[:5] == (
        "07942946765",
        "ORDER2026072512362267544904",
        "178.8",
        "NTKWJX",
        "https://www.ctexcel.com/uk/buyCard/buyCardPackage/1?recommendCode=NTKWJX",
    )
    assert row[5]


def test_ctexcel_public_card_is_distinct_and_has_copy_fields(ctexcel_client):
    client, db_path = ctexcel_client
    customer_id = _insert_ctexcel_customer(db_path)
    asyncio.run(
        crud.update_customer(
            customer_id,
            CustomerUpdate(
                phone_number="07942946765",
                ctexcel_order_number="ORDER2026072512362267544904",
                ctexcel_transaction_amount="178.8",
                ctexcel_referral_code="NTKWJX",
                ctexcel_referral_link=(
                    "https://www.ctexcel.com/uk/buyCard/buyCardPackage/1"
                    "?recommendCode=NTKWJX"
                ),
            ),
        )
    )

    response = client.get("/p/ctexcel-public-token")

    assert response.status_code == 200
    assert response.headers["X-Cache-Version"] == "4000001"
    body = response.text
    assert "CTExcel 号码与订单资料" in body
    assert "07942946765" in body
    assert "ORDER2026072512362267544904" in body
    assert "NTKWJX" in body
    assert "<!--email_off-->" in body
    assert "copyValue" in body
    assert "语音信箱" not in body
    assert "giffgaff" not in body.lower()

    version = client.get("/api/public/ctexcel-public-token/version")
    assert version.status_code == 200
    assert version.json() == {"public_version": 4_000_001}


def test_ctexcel_rejects_giffgaff_only_tools(ctexcel_client):
    client, db_path = ctexcel_client
    customer_id = _insert_ctexcel_customer(db_path)

    assert client.put(
        f"/api/customers/{customer_id}/esim-code",
        json={"code": "1$example.com$activation"},
    ).status_code == 400
    assert client.get(
        f"/api/customers/{customer_id}/payment-info-emails"
    ).status_code == 400
    assert client.post(
        f"/api/customers/{customer_id}/identity/regenerate"
    ).status_code == 400


def test_frontend_contains_persistent_ctexcel_mode_and_mode_specific_ui():
    html_text = (BACKEND_DIR.parent / "frontend" / "index.html").read_text(encoding="utf-8")

    assert "function switchAppMode(mode)" in html_text
    assert "body: JSON.stringify({ app_mode: normalized })" in html_text
    assert "product_type: appMode" in html_text
    assert 'data-mode-only="ctexcel"' in html_text
    assert 'data-mode-only="giffgaff"' in html_text
    assert "function refreshCTExcelOrderForActive" in html_text
    assert "/ctexcel-order-info" in html_text
    assert "ctexcel-50x40" in html_text
    assert "CTExcel订单号" in html_text
    assert "CTExcel推荐码" in html_text
