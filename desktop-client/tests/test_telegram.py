from __future__ import annotations

import httpx
import pytest

from ctexcel_client.config import TelegramConfig
from ctexcel_client.telegram import (
    TelegramError,
    TelegramNotifier,
    validate_telegram_config,
)


TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd"


def test_telegram_send_photo_uses_multipart_and_caption():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["content_type"] = request.headers["content-type"]
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 1}},
        )

    with TelegramNotifier(
        TelegramConfig(
            enabled=True,
            bot_token=TOKEN,
            chat_id="-1001234567890",
        ),
        transport=httpx.MockTransport(handler),
    ) as notifier:
        result = notifier.send_payment_qr(
            b"\x89PNG\r\n\x1a\nQRDATA",
            caption="CTExcel 微信付款 · 线程 3",
        )

    assert result["ok"] is True
    assert captured["path"].endswith(f"/bot{TOKEN}/sendPhoto")
    assert captured["content_type"].startswith("multipart/form-data")
    assert b"-1001234567890" in captured["body"]
    assert "线程 3".encode() in captured["body"]
    assert b"QRDATA" in captured["body"]


def test_telegram_test_message_targets_configured_chat():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json={"ok": True, "result": {}})

    with TelegramNotifier(
        TelegramConfig(
            enabled=True,
            bot_token=TOKEN,
            chat_id="@ctexcel_payments",
        ),
        transport=httpx.MockTransport(handler),
    ) as notifier:
        notifier.send_test()

    assert b"%40ctexcel_payments" in captured["body"]


def test_telegram_config_validation_rejects_incomplete_values():
    with pytest.raises(TelegramError, match="Bot Token"):
        validate_telegram_config(
            TelegramConfig(enabled=True, bot_token="short", chat_id="123")
        )
    with pytest.raises(TelegramError, match="Chat ID"):
        validate_telegram_config(
            TelegramConfig(
                enabled=True,
                bot_token=TOKEN,
                chat_id="invalid chat id",
            )
        )


def test_telegram_notifier_forces_direct_network(monkeypatch):
    captured = {}

    class DummyClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def close(self):
            pass

    monkeypatch.setattr("ctexcel_client.telegram.httpx.Client", DummyClient)

    notifier = TelegramNotifier(
        TelegramConfig(
            enabled=True,
            bot_token=TOKEN,
            chat_id="-1001234567890",
        )
    )
    notifier.close()

    assert captured["trust_env"] is False
    assert "proxy" not in captured
