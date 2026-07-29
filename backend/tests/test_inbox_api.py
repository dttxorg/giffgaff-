from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

import crud
import database
import main


@pytest.fixture
def inbox_client():
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "test.db")
        original_paths = (database.DATABASE_PATH, crud.DATABASE_PATH, main.DATABASE_PATH)
        original_password = main.APP_PASSWORD
        database.DATABASE_PATH = db_path
        crud.DATABASE_PATH = db_path
        main.DATABASE_PATH = db_path
        main.APP_PASSWORD = ""
        asyncio.run(database.init_db())
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute(
                """INSERT INTO customers
                   (email, activation_date, activation_status, email_provider_id,
                    email_account_id, moemail_address, is_moemail_auto)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    "reader@example.com",
                    "2026-07-25",
                    "未开始",
                    7,
                    "account-1",
                    "reader@example.com",
                    1,
                ),
            )
            customer_id = cursor.lastrowid
            conn.commit()
        yield TestClient(main.app), customer_id, db_path
        database.DATABASE_PATH, crud.DATABASE_PATH, main.DATABASE_PATH = original_paths
        main.APP_PASSWORD = original_password


def test_inbox_list_returns_every_summary_without_loading_bodies(inbox_client):
    client, customer_id, _ = inbox_client
    provider = MagicMock()
    provider.get_email_messages.return_value = {
        "messages": [
            {
                "id": "older",
                "subject": "Older message",
                "from": {"address": "old@example.com"},
                "receivedAt": "2026-07-24T08:00:00Z",
            },
            {
                "id": "newer",
                "subject": "Newest message",
                "fromAddress": "new@example.com",
                "sentAt": 1784918151251,
                "receivedAt": "2026-07-25T09:00:00Z",
            },
        ]
    }

    with patch.object(
        main,
        "_resolve_inbox_provider",
        new=AsyncMock(return_value=("account-1", provider)),
    ):
        response = client.get(f"/api/customers/{customer_id}/inbox")

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["email"] == "reader@example.com"
    assert data["count"] == 2
    assert [item["id"] for item in data["messages"]] == ["newer", "older"]
    assert data["messages"][0]["from_address"] == "new@example.com"
    assert data["messages"][0]["sent_at"] == "2026-07-24T18:35:51.251Z"
    assert data["messages"][1]["from_address"] == "old@example.com"
    provider.get_email_messages.assert_called_once_with("account-1")
    provider.get_message.assert_not_called()


def test_inbox_message_returns_full_plain_body_with_html_fallback(inbox_client):
    client, customer_id, _ = inbox_client
    provider = MagicMock()
    provider.get_email_messages.return_value = {
        "messages": [
            {
                "id": "message-1",
                "subject": "Account notice",
                "fromAddress": "service@example.com",
                "receivedAt": "2026-07-25T10:30:00Z",
            }
        ]
    }
    provider.get_message.return_value = {
        "message": {
            "id": "message-1",
            "to": [{"email": "reader@example.com"}],
            "headers": {"Date": "2026-07-25T10:29:00Z"},
            "html": "<p>Hello <strong>Reader</strong></p><p>Second line</p>",
        }
    }

    with patch.object(
        main,
        "_resolve_inbox_provider",
        new=AsyncMock(return_value=("account-1", provider)),
    ):
        response = client.get(
            f"/api/customers/{customer_id}/inbox-message",
            params={"message_id": "message-1"},
        )

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["id"] == "message-1"
    assert data["subject"] == "Account notice"
    assert data["from_address"] == "service@example.com"
    assert data["to_address"] == "reader@example.com"
    assert data["sent_at"] == "2026-07-25T10:29:00Z"
    assert data["received_at"] == "2026-07-25T10:30:00Z"
    assert "Hello Reader" in data["body"]
    assert "Second line" in data["body"]
    assert "<strong>" not in data["body"]
    assert "<strong>Reader</strong>" in data["html_body"]
    provider.get_message.assert_called_once_with("account-1", "message-1")


def test_inbox_message_fetches_html_even_when_summary_has_plain_text(
    inbox_client,
):
    client, customer_id, _ = inbox_client
    provider = MagicMock()
    provider.get_email_messages.return_value = {
        "messages": [
            {
                "id": "message-with-image",
                "subject": "Rich message",
                "text": "Plain summary body",
            }
        ]
    }
    provider.get_message.return_value = {
        "message": {
            "id": "message-with-image",
            "html": (
                '<a href="https://example.com/open">'
                '<img src="https://images.example.com/card.png" '
                'alt="Open card"></a>'
            ),
        }
    }

    with patch.object(
        main,
        "_resolve_inbox_provider",
        new=AsyncMock(return_value=("account-1", provider)),
    ):
        response = client.get(
            f"/api/customers/{customer_id}/inbox-message",
            params={"message_id": "message-with-image"},
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["body"] == "Plain summary body"
    assert 'href="https://example.com/open"' in data["html_body"]
    assert 'src="https://images.example.com/card.png"' in data["html_body"]
    provider.get_message.assert_called_once_with(
        "account-1",
        "message-with-image",
    )


def test_inbox_message_recognizes_html_returned_in_text_field(inbox_client):
    client, customer_id, _ = inbox_client
    provider = MagicMock()
    provider.get_email_messages.return_value = {
        "messages": [
            {
                "id": "html-in-text",
                "subject": "Provider HTML",
                "text": (
                    '<p>Hello <a href="https://example.com">website</a></p>'
                ),
            }
        ]
    }
    provider.get_message.return_value = {}

    with patch.object(
        main,
        "_resolve_inbox_provider",
        new=AsyncMock(return_value=("account-1", provider)),
    ):
        response = client.get(
            f"/api/customers/{customer_id}/inbox-message",
            params={"message_id": "html-in-text"},
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["body"] == "Hello website"
    assert '<a href="https://example.com">' in data["html_body"]


def test_inbox_message_missing_returns_404(inbox_client):
    client, customer_id, _ = inbox_client
    provider = MagicMock()
    provider.get_email_messages.return_value = {"messages": []}
    provider.get_message.return_value = {}

    with patch.object(
        main,
        "_resolve_inbox_provider",
        new=AsyncMock(return_value=("account-1", provider)),
    ):
        response = client.get(
            f"/api/customers/{customer_id}/inbox-message",
            params={"message_id": "missing"},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "邮件不存在或已失效"


def test_replacing_customer_email_persists_provider_route(inbox_client):
    _, customer_id, db_path = inbox_client

    async def fake_generate_email_account(*, manual_provider_id=None, manual_domain=None):
        return {
            "email": "new@example.com",
            "email_account_id": "new-account",
            "email_provider_id": 9,
            "email_provider_domain": "example.com",
            "share_link": "",
            "is_email_auto": True,
        }

    with patch.object(main, "_generate_email_account", fake_generate_email_account):
        result = asyncio.run(main.create_customer_moemail(customer_id, main.MoEmailCreateRequest()))

    assert result["email_provider_id"] == 9
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """SELECT email, moemail_id, email_provider_id, email_account_id,
                      email_provider_domain
               FROM customers WHERE id = ?""",
            (customer_id,),
        ).fetchone()
    assert row == (
        "new@example.com",
        "new-account",
        9,
        "new-account",
        "example.com",
    )


def test_payment_query_persists_before_returning_success(inbox_client):
    client, customer_id, db_path = inbox_client
    provider = MagicMock()
    provider.get_email_messages.return_value = {
        "messages": [
            {
                "id": "payment-1",
                "subject": "Your payment info has changed",
            }
        ]
    }
    provider.get_message.return_value = {
        "message": {
            "id": "payment-1",
            "text": "Your payment info has changed",
        }
    }

    with patch.object(
        main,
        "_resolve_inbox_provider",
        new=AsyncMock(return_value=("account-1", provider)),
    ):
        response = client.get(f"/api/customers/{customer_id}/payment-info-emails")

    assert response.status_code == 200, response.text
    assert response.json()["changed_found"] is True
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """SELECT payment_changed_at, payment_last_checked_at
               FROM customers WHERE id = ?""",
            (customer_id,),
        ).fetchone()
    assert row[0]
    assert row[1]
    assert row[0] == row[1]


def test_payment_query_reports_database_write_failure(inbox_client):
    client, customer_id, _ = inbox_client
    provider = MagicMock()
    provider.get_email_messages.return_value = {
        "messages": [
            {
                "id": "payment-1",
                "subject": "Your payment info has changed",
                "receivedAt": "2026-07-25T12:00:00Z",
            }
        ]
    }
    provider.get_message.return_value = {
        "message": {"text": "Your payment info has changed"}
    }

    with patch.object(
        main,
        "_resolve_inbox_provider",
        new=AsyncMock(return_value=("account-1", provider)),
    ), patch.object(
        main,
        "save_payment_check_result",
        new=AsyncMock(side_effect=RuntimeError("database offline")),
    ):
        response = client.get(f"/api/customers/{customer_id}/payment-info-emails")

    assert response.status_code == 502
    assert "database offline" in response.json()["detail"]
