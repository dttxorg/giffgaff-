from __future__ import annotations

import asyncio
import base64
import sqlite3
import sys
import tempfile
from datetime import date
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

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
            assert "ctexcel_login_account" in columns
            assert "ctexcel_initial_password" in columns
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
**eSIM 信息**
订单号：ORDER2026082411300999197112
eSIM ICCID：89443052936071356920
eSIM手机号：447529292998
激活码：1$sm-v4-010-a-gtm.pr.go-esim.com$AEF6BEA9549C6274D6143175ACD15119
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
    assert data["esim_lpa"] == (
        "LPA:1$sm-v4-010-a-gtm.pr.go-esim.com$"
        "AEF6BEA9549C6274D6143175ACD15119"
    )
    assert data["received_at"] == "2026-07-24T18:35:51.251Z"
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """SELECT phone_number, ctexcel_order_number,
                      ctexcel_transaction_amount, ctexcel_referral_code,
                      ctexcel_referral_link, esim_raw_code,
                      ctexcel_last_checked_at
               FROM customers WHERE id = ?""",
            (customer_id,),
        ).fetchone()
    assert row[:6] == (
        "07942946765",
        "ORDER2026072512362267544904",
        "178.8",
        "NTKWJX",
        "https://www.ctexcel.com/uk/buyCard/buyCardPackage/1?recommendCode=NTKWJX",
        "1$sm-v4-010-a-gtm.pr.go-esim.com$AEF6BEA9549C6274D6143175ACD15119",
    )
    assert row[6]


def test_ctexcel_freecard_order_email_parses_new_order_and_payment_amount():
    parsed = main._extract_ctexcel_order_info(
        {
            "subject": "CTExcel 预存领卡成功",
            "text": (
                "订单号：ORDERSUK2026073104095817734376\n"
                "付款金额：£1.00\n"
                "手机号码：07942946765"
            ),
        }
    )

    assert parsed["order_number"] == "ORDERSUK2026073104095817734376"
    assert parsed["transaction_amount"] == "1.00"
    assert parsed["phone_number"] == "07942946765"


def test_ctexcel_esim_email_parses_raw_code_for_lpa_completion():
    parsed = main._extract_ctexcel_order_info(
        {
            "subject": "CTExcel eSIM 订单资料",
            "text": (
                "订单号：ORDER2026082411300999197112\n"
                "eSIM ICCID：89443052936071356920\n"
                "eSIM手机号：447529292998\n"
                "激活码：1$sm-v4-010-a-gtm.pr.go-esim.com$"
                "AEF6BEA9549C6274D6143175ACD15119"
            ),
        }
    )

    assert parsed["esim_raw_code"] == (
        "1$sm-v4-010-a-gtm.pr.go-esim.com$"
        "AEF6BEA9549C6274D6143175ACD15119"
    )
    assert parsed["phone_number"] == "447529292998"
    assert main._build_esim_lpa(parsed["esim_raw_code"]) == (
        "LPA:1$sm-v4-010-a-gtm.pr.go-esim.com$"
        "AEF6BEA9549C6274D6143175ACD15119"
    )


def test_ctexcel_activation_email_parses_login_with_special_character_password():
    parsed = main._extract_ctexcel_order_info(
        {
            "subject": "【CTExcel】个人中心账户已开通",
            "htmlBody": (
                "<p>您距离成功注册个人中心账户只差一步。</p>"
                "<div>账号：<strong>447900000123</strong></div>"
                "<div>密码: <strong>A7b9*XyZ</strong></div>"
                "<div>个人中心地址:"
                "https://www.ctexcel.com/uk/login?redirect=/personal/personalHome"
                "</div>"
            ),
        }
    )

    assert parsed["login_account"] == "447900000123"
    assert parsed["initial_password"] == "A7b9*XyZ"


@pytest.mark.parametrize(
    "text",
    [
        "账号:447900000123\n个人中心地址:https://www.ctexcel.com/uk/login?redirect=/personal/personalHome",
        "密码:A7b9*XyZ\n个人中心地址:https://www.ctexcel.com/uk/login?redirect=/personal/personalHome",
        "普通网站账号:447900000123\n密码:A7b9*XyZ",
    ],
)
def test_ctexcel_login_parser_requires_both_fields_and_portal_context(text):
    parsed = main._extract_ctexcel_order_info({"subject": "普通通知", "text": text})

    assert parsed["login_account"] is None
    assert parsed["initial_password"] is None


def test_ctexcel_activation_email_syncs_credentials_and_bumps_cache_once(
    ctexcel_client,
):
    client, db_path = ctexcel_client
    customer_id = _insert_ctexcel_customer(db_path)
    provider = MagicMock(name="activation-provider")
    provider.get_email_messages.return_value = {
        "messages": [
            {
                "id": "activation-mail",
                "subject": "【CTExcel】个人中心账户已开通",
                "fromAddress": "service@ctexcel.example",
                "receivedAt": 1785567600000,
            }
        ]
    }
    provider.get_message.return_value = {
        "message": {
            "text": (
                "账号:447900000123\n"
                "密码:A7b9*XyZ\n"
                "个人中心地址:"
                "https://www.ctexcel.com/uk/login?redirect=/personal/personalHome"
            )
        }
    }

    with patch.object(
        main,
        "_resolve_inbox_provider",
        new=AsyncMock(return_value=("mail-account-1", provider)),
    ):
        first = client.get(f"/api/customers/{customer_id}/ctexcel-order-info")
        second = client.get(f"/api/customers/{customer_id}/ctexcel-order-info")

    assert first.status_code == 200, first.text
    assert first.json()["login_account"] == "447900000123"
    assert first.json()["initial_password"] == "A7b9*XyZ"
    assert second.status_code == 200
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """SELECT ctexcel_login_account, ctexcel_initial_password,
                      public_token, public_version
               FROM customers WHERE id = ?""",
            (customer_id,),
        ).fetchone()
    assert row == (
        "447900000123",
        "A7b9*XyZ",
        "ctexcel-public-token",
        2,
    )


def test_ctexcel_confirmation_subject_marks_registration_success(
    ctexcel_client,
):
    client, db_path = ctexcel_client
    customer_id = _insert_ctexcel_customer(db_path)
    provider = MagicMock(name="confirmation-provider")
    provider.get_email_messages.return_value = {
        "messages": [
            {
                "id": "confirmation-mail",
                "subject": "【CTExcel】您的订单已确认！",
                "fromAddress": "service@ctexcel.example",
                "receivedAt": 1785481500000,
            }
        ]
    }
    provider.get_message.return_value = {
        "message": {
            "text": "感谢您领取中国电信CTExcel英国卡，您的订单已经确认。"
        }
    }

    with patch.object(
        main,
        "_resolve_inbox_provider",
        new=AsyncMock(return_value=("mail-account-1", provider)),
    ):
        response = client.get(
            f"/api/customers/{customer_id}/ctexcel-order-info"
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["found"] is True
    assert data["registration_confirmed"] is True
    assert data["registration_confirmed_at"]
    assert data["order_number"] is None
    assert data["phone_number"] is None
    assert data["subject"] == "【CTExcel】您的订单已确认！"
    provider.get_message.assert_not_called()
    with sqlite3.connect(db_path) as connection:
        confirmed_at = connection.execute(
            """SELECT ctexcel_registration_confirmed_at
               FROM customers WHERE id = ?""",
            (customer_id,),
        ).fetchone()[0]
    assert confirmed_at == data["registration_confirmed_at"]


def test_ctexcel_confirmation_subject_match_is_specific():
    assert main._is_ctexcel_registration_confirmation(
        {"subject": "【CTExcel】您的订单已确认！"}
    )
    assert not main._is_ctexcel_registration_confirmation(
        {"subject": "【CTExcel】您的邮箱验证码"}
    )


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
                ctexcel_login_account="447900000123",
                ctexcel_initial_password="A7b9*XyZ",
            ),
        )
    )

    response = client.get("/p/ctexcel-public-token")

    assert response.status_code == 200
    assert response.headers["X-Cache-Version"] == "14000002"
    body = response.text
    assert "CTExcel 已激活号码资料" in body
    assert "07942946765" in body
    assert "ctexcel@example.com" not in body
    assert "注册邮箱" not in body
    assert 'id="email-row"' not in body
    assert "447900000123" in body
    assert "A7b9*XyZ" in body
    assert "个人中心登录资料" in body
    assert "一键复制登录资料" in body
    assert "ChatGPT Plus / Pro 代充" in body
    assert "无需海外支付方式" in body
    assert "ChatGPT 5x Pro" in body
    assert "ChatGPT 20x Pro" in body
    assert "需要 GPT 代充" in body
    assert "修改个人信息和邮箱" in body
    assert "打开我的账户" in body
    assert "点击修改并保存" in body
    assert 'class="profile-media"' in body
    assert 'width="800" height="382"' in body
    assert "CTExcel 修改个人信息和邮箱教程" in body
    profile_image = body.split(
        '<div class="profile-media"><img src="data:image/webp;base64,', 1
    )[1].split('" width="800" height="382"', 1)[0]
    with Image.open(BytesIO(base64.b64decode(profile_image))) as decoded:
        decoded.verify()
    with Image.open(BytesIO(base64.b64decode(profile_image))) as decoded:
        assert decoded.format == "WEBP"
        assert decoded.size == (800, 382)
    assert "04 / SIM CARD ID" in body
    assert "05 / CHINA ROAMING" in body
    assert "06 / NUMBER PORTING" in body
    assert "请保存好 ICCID" in body
    assert "申请补卡时需要使用它" in body
    assert "重要提醒：现在拍照并保存 ICCID" in body
    assert 'aria-label="ICCID 补卡重要提醒"' in body
    assert 'class="iccid-media"' in body
    assert 'width="600" height="379"' in body
    iccid_image = body.split(
        '<div class="iccid-media"><img src="data:image/png;base64,', 1
    )[1].split('" width="600" height="379"', 1)[0]
    with Image.open(BytesIO(base64.b64decode(iccid_image))) as decoded:
        decoded.verify()
    with Image.open(BytesIO(base64.b64decode(iccid_image))) as decoded:
        assert decoded.format == "PNG"
        assert decoded.size == (600, 379)
    assert "到手即可用" in body
    assert "重要提醒：不要接听任何电话" in body
    assert "CTExcel 官方不会通过电话通知任何事项" in body
    assert "接听任何来电都会按中国大陆漫游资费收费" in body
    assert "使用中国大陆手机拨打 CTExcel 国内客服电话" in body
    assert "400 828 1800" in body
    assert 'aria-label="CTExcel 官方来电重要提醒"' in body
    assert "中国大陆漫游资费" in body
    assert "中国大陆 · 区域 2" in body
    assert "£0.05" in body
    assert "£0.20" in body
    assert "£0.10" in body
    assert "£0.005" in body
    assert "1GB 按 1024MB" in body
    assert "https://www.ctexcel.com/uk/tariffQuery/0" in body
    assert "giffgaff 携号转网教程" in body
    assert body.count('class="porting-step"') == 3
    assert body.count('class="porting-media"') == 3
    assert body.count("data:image/webp;base64,") == 4
    assert body.count("data:image/png;base64,") == 1
    assert body.count('width="800"') == 4
    assert "PAC 码" in body
    assert "1–3 个工作日" in body
    assert "转网期间信号可能短暂中断" in body
    assert "__PORTING_STEP_" not in body
    assert "__PERSONAL_INFO_GUIDE_IMAGE__" not in body
    assert (
        'href="https://www.ctexcel.com/uk/login?redirect=/personal/personalHome"'
        in body
    )
    assert "<!--email_off-->" not in body
    assert "copyValue" in body
    assert "ORDER2026072512362267544904" not in body
    assert "NTKWJX" not in body
    assert "178.8" not in body
    assert "CTExcel 订单号" not in body
    assert "专属推荐码" not in body
    assert "打开专属推荐链接" not in body
    assert "交易金额" not in body
    assert "订单服务" not in body
    assert "语音信箱" not in body
    assert "原 giffgaff 手机号" in body

    version = client.get("/api/public/ctexcel-public-token/version")
    assert version.status_code == 200
    assert version.json() == {"public_version": 14_000_002}


def test_ctexcel_admin_edit_keeps_public_token_and_bumps_only_on_change(
    ctexcel_client,
):
    client, db_path = ctexcel_client
    customer_id = _insert_ctexcel_customer(db_path)
    before = client.get(f"/api/customers/{customer_id}").json()
    payload = {
        "ctexcel_login_account": "447900000456",
        "ctexcel_initial_password": "Q2w3*ErT",
    }

    first = client.patch(f"/api/customers/{customer_id}", json=payload)
    after = client.get(f"/api/customers/{customer_id}").json()
    repeated = client.patch(f"/api/customers/{customer_id}", json=payload)
    final = client.get(f"/api/customers/{customer_id}").json()

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert after["ctexcel_login_account"] == "447900000456"
    assert after["ctexcel_initial_password"] == "Q2w3*ErT"
    assert after["public_token"] == before["public_token"]
    assert after["public_version"] == before["public_version"] + 1
    assert final["public_token"] == before["public_token"]
    assert final["public_version"] == after["public_version"]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT public_token FROM customers WHERE id = ?",
            (customer_id,),
        ).fetchone()[0] == "ctexcel-public-token"


def test_ctexcel_public_card_shows_pending_login_state_without_credentials(
    ctexcel_client,
):
    client, db_path = ctexcel_client
    _insert_ctexcel_customer(db_path)

    response = client.get("/p/ctexcel-public-token")

    assert response.status_code == 200
    assert response.headers["X-Cache-Version"] == "14000001"
    assert "登录资料等待同步" in response.text
    assert "一键复制登录资料</button>" in response.text
    assert "disabled" in response.text
    assert (
        'href="https://www.ctexcel.com/uk/login?redirect=/personal/personalHome"'
        in response.text
    )


def test_legacy_ctexcel_customer_can_lazily_create_fixed_credential_page(
    ctexcel_client,
):
    client, db_path = ctexcel_client
    customer_id = _insert_ctexcel_customer(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """UPDATE customers
               SET public_token = NULL,
                   ctexcel_login_account = ?,
                   ctexcel_initial_password = ?
               WHERE id = ?""",
            ("447900000789", "Z9x8*WvU", customer_id),
        )
        connection.commit()

    first = client.post(f"/api/customers/{customer_id}/public-link/ensure")
    second = client.post(f"/api/customers/{customer_id}/public-link/ensure")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    token = first.json()["public_token"]
    assert token
    page = client.get(f"/p/{token}")
    assert page.status_code == 200
    assert page.headers["X-Cache-Version"] == "14000001"
    assert "个人中心登录资料" in page.text
    assert "447900000789" in page.text
    assert "Z9x8*WvU" in page.text
    assert "一键复制登录资料" in page.text


def test_ctexcel_supports_esim_but_rejects_other_giffgaff_only_tools(ctexcel_client):
    client, db_path = ctexcel_client
    customer_id = _insert_ctexcel_customer(db_path)

    esim = client.put(
        f"/api/customers/{customer_id}/esim-code",
        json={"code": "1$example.com$activation"},
    )
    assert esim.status_code == 200, esim.text
    assert esim.json()["esim_raw_code"] == "1$example.com$activation"
    qr = client.get(f"/api/customers/{customer_id}/esim-qr.png")
    assert qr.status_code == 200
    assert qr.headers["X-LPA-String"] == "LPA:1$example.com$activation"
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
    assert "CTExcel 已激活号码资料扫码页" in html_text
    assert "打开已激活号码资料扫码页" in html_text
    assert "复制扫码页链接" in html_text
    assert "function ensureCustomerPublicLink" in html_text
    assert "function openCustomerPublicPage" in html_text
    assert "function copyCustomerPublicPage" in html_text
    assert "/public-link/ensure" in html_text
    assert "const PUBLIC_PAGE_VIEW_VERSION = '11';" in html_text
    assert "客户扫码后可快捷复制手机号、个人中心账号和初始密码；注册邮箱不会显示在扫码页。" in html_text
    assert "初始密码、订单号和推荐码" not in html_text
    assert "扫码页" in html_text
    assert "CTExcel订单号" in html_text
    assert "CTExcel推荐码" in html_text
    assert "ctexcel-login-form" in html_text
    assert "ctexcel_login_account: loginAccount" in html_text
    assert "ctexcel_initial_password: initialPassword" in html_text
    assert "eSIM 完整 LPA" in html_text
    assert "d-ctexcel-esim-lpa" in html_text
    assert "已复制 eSIM LPA" in html_text
    assert "function buildEsimLpa" in html_text
    assert (
        "https://www.ctexcel.com/uk/login?redirect=/personal/personalHome"
        in html_text
    )


def test_legacy_label_config_is_merged_with_ctexcel_template(ctexcel_client):
    client, _ = ctexcel_client
    asyncio.run(
        crud.set_setting(
            "label_templates",
            """[{
              "id": "legacy-giffgaff-only",
              "name": "旧 Giffgaff 模板",
              "width_mm": 50,
              "height_mm": 30,
              "elements": []
            }]""",
        )
    )

    response = client.get("/api/label-config")

    assert response.status_code == 200
    templates = response.json()["templates"]
    ids = {template["id"] for template in templates}
    assert "legacy-giffgaff-only" in ids
    assert "ctexcel-50x40" in ids
    ctexcel = next(
        template for template in templates if template["id"] == "ctexcel-50x40"
    )
    sources = {element["source"] for element in ctexcel["elements"]}
    assert {"手机号", "邮箱", "CTExcel订单号", "CTExcel推荐码"} <= sources
    assert "号码资料二维码" in sources


def test_ctexcel_auto_sync_claims_recent_incomplete_mailbox_once(
    ctexcel_client,
):
    _, db_path = ctexcel_client
    with sqlite3.connect(db_path) as connection:
        pending_id = connection.execute(
            """INSERT INTO customers
               (product_type, email, activation_date, email_account_id)
               VALUES ('ctexcel', 'pending@example.com', '2026-07-29', 'pending-mail')"""
        ).lastrowid
        connection.execute(
            """INSERT INTO customers
               (product_type, email, phone_number, activation_date,
                email_account_id, ctexcel_order_number,
                ctexcel_login_account, ctexcel_initial_password)
               VALUES ('ctexcel', 'complete@example.com', '07900000001',
                       '2026-07-29', 'complete-mail', 'ORDER-COMPLETE',
                       '447900000111', 'Complete*Pass1')"""
        )
        connection.execute(
            """INSERT INTO customers
               (product_type, email, activation_date, email_account_id,
                ctexcel_order_number, ctexcel_registration_confirmed_at)
               VALUES ('ctexcel', 'confirmed@example.com', '2026-07-29',
                       'confirmed-mail', 'ORDER-CONFIRMED',
                       '2026-07-31T08:00:00Z')"""
        )
        connection.execute(
            """INSERT INTO customers
               (product_type, email, activation_date, email_account_id)
               VALUES ('giffgaff', 'giffgaff@example.com',
                       '2026-07-29', 'giffgaff-mail')"""
        )
        connection.execute(
            """INSERT INTO customers
               (product_type, email, activation_date)
               VALUES ('ctexcel', 'manual@example.com', '2026-07-29')"""
        )
        connection.execute(
            """INSERT INTO customers
               (product_type, email, activation_date, email_account_id, created_at)
               VALUES ('ctexcel', 'old@example.com', '2026-06-01',
                       'old-mail', datetime('now', '-30 days'))"""
        )
        connection.commit()

    async def claim_from_two_workers():
        return await asyncio.gather(
            main._claim_pending_ctexcel_auto_sync_customers(),
            main._claim_pending_ctexcel_auto_sync_customers(),
        )

    claims = asyncio.run(claim_from_two_workers())

    assert sorted(len(batch) for batch in claims) == [0, 2]
    claimed_ids = [row["id"] for batch in claims for row in batch]
    assert pending_id in claimed_ids
    assert len(claimed_ids) == 2
    with sqlite3.connect(db_path) as connection:
        claimed_at = connection.execute(
            "SELECT ctexcel_last_checked_at FROM customers WHERE id = ?",
            (pending_id,),
        ).fetchone()[0]
    assert claimed_at


def test_ctexcel_auto_sync_round_isolates_mailbox_failures():
    pending = [{"id": 101}, {"id": 102}]
    success = main.CTExcelOrderInfoOut(found=True)
    sync_mock = AsyncMock(
        side_effect=[success, main.HTTPException(status_code=502, detail="mail down")]
    )

    with patch.object(
        main,
        "_claim_pending_ctexcel_auto_sync_customers",
        new=AsyncMock(return_value=pending),
    ), patch.object(main, "_sync_ctexcel_order_info", new=sync_mock):
        result = asyncio.run(main._ctexcel_auto_sync_once())

    assert result == {"checked": 2, "synced": 1, "failed": 1}
    assert sync_mock.await_count == 2
