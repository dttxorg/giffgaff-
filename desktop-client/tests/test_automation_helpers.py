from decimal import Decimal
from datetime import datetime, timedelta, timezone
import binascii
from pathlib import Path
import os
import struct
import threading
import time
from types import SimpleNamespace
import zlib

import pytest

import ctexcel_client.automation as automation_module

from ctexcel_client.automation import (
    AutomationResult,
    AutomationError,
    CTExcelBatchAutomation,
    CTExcelAutomation,
    PaymentQrNotDetectedError,
    RetryableBlankPageError,
    RetryableProxyBrowserError,
    RetryableStalledPageError,
    application_target,
    address_region_token,
    append_address_suffix,
    registration_values_for_ordinal,
    assess_verification_freshness,
    cleanup_stale_browser_profiles,
    coupon_rejection_message,
    diagnostic_response_excerpt,
    expected_route_price,
    normalize_money,
    normalize_gateway_amount,
    parse_message_timestamp,
    is_payment_success_url,
    is_payment_gateway_url,
    is_wechat_payment_url,
    payment_page_has_expected_amount,
    payment_page_content_is_ready,
    page_progress_fingerprint,
    page_url_matches_path,
    is_ctexcel_url,
    price_is_expected,
    proxy_browser_error_reason,
    recent_verification_code,
    browser_startup_snapshot_is_blank,
    tunnel_browser_start_delay,
    verification_cooldown_message,
)
from ctexcel_client.config import (
    AppConfig,
    PAYMENT_METHOD_ALIPAY,
    PAYMENT_METHOD_WECHAT,
    PURCHASE_ROUTE_50GB,
    PURCHASE_ROUTE_FREECARD,
    PURCHASE_ROUTE_RETURN_HOME_15GB,
    ProxyConfig,
    RegistrationDefaults,
)
from ctexcel_client.proxy import ProxyError


def test_money_and_discount_price_parsing():
    assert normalize_money("£ 5.95") == Decimal("5.95")
    assert normalize_money("not-a-price") is None
    assert price_is_expected("订单金额：£5.95", "5.95") is True
    assert price_is_expected("订单金额：£11.90", "5.95") is False
    assert payment_page_has_expected_amount(
        "请使用微信扫描二维码 ¥9.11(1GBP)",
        "1.00",
    )


def test_gateway_amount_is_canonical_and_rejects_ambiguous_values():
    assert normalize_gateway_amount("£5,9") == "5.90"
    assert normalize_gateway_amount("11 GBP") == "11.00"
    with pytest.raises(AutomationError, match="支付网关预期金额格式错误"):
        normalize_gateway_amount("5.95 USD")


def test_alipay_helpers_timeout_when_gateway_never_reaches_expected_state(
    monkeypatch,
):
    clock = {"value": 0.0}
    monkeypatch.setattr(
        automation_module.time,
        "monotonic",
        lambda: clock["value"],
    )

    class FakePage:
        def evaluate(self, _script, *_args):
            return {"found": False, "enabled": False}

    runner = CTExcelAutomation(
        AppConfig(),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    monkeypatch.setattr(
        runner,
        "_wait_interruptibly",
        lambda seconds: clock.__setitem__(
            "value", clock["value"] + seconds
        ),
    )
    with pytest.raises(RetryableStalledPageError):
        runner._select_alipay_gateway_option(FakePage())

    class DisabledPage:
        def evaluate(self, _script, *_args):
            return {"found": True, "enabled": False}

    clock["value"] = 0.0
    with pytest.raises(RetryableStalledPageError):
        runner._click_alipay_gateway_pay(
            DisabledPage(),
            expected_amount="£5,95",
        )


def test_return_home_15gb_route_requires_eleven_pounds_after_coupon():
    config = AppConfig(
        purchase_route=PURCHASE_ROUTE_RETURN_HOME_15GB,
        registration=RegistrationDefaults(
            expected_price_gbp="5.95",
        ),
    )

    assert expected_route_price(config) == "11.00"
    assert price_is_expected("订单金额：£11.00", expected_route_price(config))


def test_return_home_plan_selection_scripts_cover_tab_card_and_details():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "automation.py"
    ).read_text(encoding="utf-8")

    assert "SELECT_RETURN_HOME_TAB_SCRIPT" in source
    assert "SELECT_RETURN_HOME_15GB_PLAN_SCRIPT" in source
    assert "回国年套餐" in source
    assert "15GB" in source
    assert "£22.00" in source
    assert "RETURN_HOME_PLAN_DETAILS_READY_SCRIPT" in source
    assert "RETURN_HOME_APPLICATION_URL" in source


def test_both_purchase_routes_recognize_their_success_page():
    assert is_payment_success_url(
        "https://www.ctexcel.com/freecard/activityPageSuccess"
    )
    assert is_payment_success_url(
        "https://www.ctexcel.com/uk/buycard/buycardsucceed"
    )
    assert not is_payment_success_url(
        "https://www.ctexcel.com/freecard/buycardWX"
    )


def test_wechat_payment_url_matches_same_tab_and_popup_routes():
    assert is_wechat_payment_url(
        "https://www.ctexcel.com/freecard/buycardWX?order=1",
        PURCHASE_ROUTE_FREECARD,
    )
    assert is_wechat_payment_url(
        "https://www.ctexcel.com/uk/buycard/buycardWX",
        PURCHASE_ROUTE_50GB,
    )
    assert is_wechat_payment_url(
        "https://www.ctexcel.com/uk/buycard/buycardWX",
        PURCHASE_ROUTE_RETURN_HOME_15GB,
    )
    assert not is_wechat_payment_url(
        "https://www.ctexcel.com/freecard/activityPageconfirm",
        PURCHASE_ROUTE_FREECARD,
    )


def test_payment_gateway_url_allows_only_https_configured_hosts():
    assert is_payment_gateway_url(
        "https://www.ctexcel.com/uk/buycard/buycardsucceed"
    )
    assert is_payment_gateway_url(
        "https://na.gateway.spring.citi.com/checkout"
    )
    assert is_payment_gateway_url(
        "https://redirect-euw1.ppro.com/payment/SESSION"
    )
    assert is_payment_gateway_url("https://pay.alipay.com/gateway")
    assert not is_payment_gateway_url("http://na.gateway.spring.citi.com/checkout")
    assert not is_payment_gateway_url("https://evil.example.test/payment")


def test_success_page_completes_immediately_without_reading_phone():
    class FakePage:
        url = "https://www.ctexcel.com/freecard/activityPageSuccess"

        def __getattr__(self, name):
            raise AssertionError(f"成功页不应读取页面内容：{name}")

    class FakeApi:
        def __init__(self):
            self.saved = None

        def save_payment_checkpoint(self, customer_id, **fields):
            self.saved = (customer_id, fields)
            return {"ok": True}

    messages = []
    api = FakeApi()
    automation = CTExcelAutomation(
        AppConfig(),
        log=messages.append,
        stage=lambda _message: None,
        customer_created=lambda _payload: None,
    )

    result = automation._wait_for_payment_success(
        FakePage(),
        api=api,
        customer_id=480,
        email="customer@example.test",
        pending_order={
            "order_number": "ORDERSUK2026073106180627794025",
            "transaction_amount": "1.00",
        },
    )

    assert result.phone_number == ""
    assert result.transaction_amount == "1.00"
    assert api.saved == (
        480,
        {
            "order_number": "ORDERSUK2026073106180627794025",
            "transaction_amount": "1.00",
            "payment_succeeded": True,
        },
    )
    assert any("客户端不再读取手机号" in item for item in messages)


def test_payment_qr_message_id_is_saved_and_deleted(monkeypatch):
    calls = []

    class FakeNotifier:
        def __init__(self, _config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def send_payment_qr(self, _image, *, caption):
            calls.append(("send", caption))
            return {"ok": True, "result": {"message_id": 73}}

        def delete_message(self, message_id):
            calls.append(("delete", message_id))
            return {"ok": True, "result": True}

    monkeypatch.setattr(automation_module, "TelegramNotifier", FakeNotifier)
    config = AppConfig()
    config.telegram.enabled = True
    runner = CTExcelAutomation(
        config,
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    monkeypatch.setattr(runner, "_capture_payment_qr", lambda _page: b"PNG")

    runner._push_payment_qr(
        object(),
        customer_id=480,
        email="customer@example.test",
        pending_order={
            "order_number": "ORDERSUK2026073106180627794025",
            "transaction_amount": "1.00",
            "payment_method": PAYMENT_METHOD_ALIPAY,
        },
    )

    assert runner.payment_qr_message_id == 73
    runner._delete_payment_qr("测试清理")
    assert runner.payment_qr_message_id is None
    assert calls[0][0] == "send"
    assert "支付宝" in calls[0][1]
    assert calls[1] == ("delete", 73)


def test_payment_qr_not_detected_skips_telegram_without_page_capture(monkeypatch):
    calls = []

    class UnexpectedNotifier:
        def __init__(self, _config):
            raise AssertionError("二维码未识别时不应创建 Telegram 客户端")

    monkeypatch.setattr(automation_module, "TelegramNotifier", UnexpectedNotifier)
    config = AppConfig()
    config.telegram.enabled = True
    runner = CTExcelAutomation(
        config,
        log=calls.append,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    def not_detected(_page):
        raise PaymentQrNotDetectedError("未检测到有效二维码")

    monkeypatch.setattr(runner, "_capture_payment_qr", not_detected)

    runner._push_payment_qr(
        object(),
        customer_id=480,
        email="customer@example.test",
        pending_order={
            "order_number": "ORDER-QR-NOT-READY",
            "transaction_amount": "5.95",
            "payment_method": PAYMENT_METHOD_ALIPAY,
        },
    )

    assert runner.payment_qr_message_id is None
    assert any("未发送付款图片" in item for item in calls)


def test_alipay_qr_capture_uses_small_codes_without_a_long_fallback_wait():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "automation.py"
    ).read_text(encoding="utf-8")

    assert "rect.width >= 64" in source
    assert "height < 64" in source
    assert "PAYMENT_METHOD_ALIPAY" in source


def test_alipay_qr_capture_prefers_qr_container_over_guide_image(monkeypatch):
    class FakeCandidate:
        def __init__(self, marker, image, size):
            self.metadata = {
                "marker": marker,
                "context": "",
                "tag": "img",
                "descendants": 0,
            }
            self.image = image
            self.size = size

        def is_visible(self):
            return True

        def bounding_box(self):
            return {"width": self.size, "height": self.size}

        def evaluate(self, _script):
            return self.metadata

        def screenshot(self, **_kwargs):
            return self.image

    class FakeCandidateList:
        def __init__(self, candidates):
            self.candidates = candidates

        def count(self):
            return len(self.candidates)

        def nth(self, index):
            return self.candidates[index]

    class FakeDocument:
        url = "https://pay.alipay.com/gateway"

        def __init__(self, candidates):
            self.candidates = candidates

        def locator(self, selector):
            if selector == "body":
                return SimpleNamespace(inner_text=lambda **_kwargs: "支付宝")
            return FakeCandidateList(self.candidates)

    class FakePage(FakeDocument):
        def screenshot(self, **_kwargs):
            return b"FULL-PAYMENT-PAGE"

    guide = FakeCandidate("alipay-guide data:image/png;base64,guide", b"GUIDE", 180)
    qr = FakeCandidate("qr-code data:image/png;base64,qr", b"QR", 120)
    page = FakePage([guide, qr])
    runner = CTExcelAutomation(
        AppConfig(),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    monkeypatch.setattr(
        runner,
        "_qr_visual_score",
        lambda image: 1.0 if image == b"QR" else 0.1,
    )

    assert runner._capture_payment_qr(page) == b"QR"


def test_qr_visual_score_distinguishes_black_white_modules_from_gray_guide():
    def png(fill):
        width = height = 64
        rows = []
        for y in range(height):
            row = bytearray([0])
            for x in range(width):
                if fill == "qr":
                    value = 0 if (x // 2 + y // 2) % 2 else 255
                else:
                    value = 128
                row.extend((value, value, value, 255))
            rows.append(bytes(row))

        def chunk(kind, value):
            return (
                struct.pack(">I", len(value))
                + kind
                + value
                + struct.pack(">I", binascii.crc32(kind + value) & 0xFFFFFFFF)
            )

        body = b"".join(rows)
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(body))
            + chunk(b"IEND", b"")
        )

    qr_score = CTExcelAutomation._qr_visual_score(png("qr"))
    guide_score = CTExcelAutomation._qr_visual_score(png("guide"))
    assert qr_score >= 0.35
    assert guide_score == 0.0


def test_alipay_qr_capture_skips_full_page_when_only_guide_is_detected(
    monkeypatch,
):
    state = {"full_page_calls": 0}

    class FakeCandidate:
        def is_visible(self):
            return True

        def bounding_box(self):
            return {"width": 180, "height": 180}

        def evaluate(self, _script):
            return {
                "marker": "alipay-guide data:image/png;base64,guide",
                "context": "",
                "tag": "img",
                "descendants": 0,
            }

        def screenshot(self, **_kwargs):
            return b"GUIDE"

    class FakeCandidateList:
        def count(self):
            return 1

        def nth(self, _index):
            return FakeCandidate()

    class FakePage:
        url = "https://pay.alipay.com/gateway"
        frames = ()

        def locator(self, selector):
            if selector == "body":
                return SimpleNamespace(inner_text=lambda **_kwargs: "支付宝")
            return FakeCandidateList()

        def screenshot(self, **_kwargs):
            state["full_page_calls"] += 1
            return b"FULL-PAYMENT-PAGE"

    runner = CTExcelAutomation(
        AppConfig(),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    clock = {"value": 0.0}
    monkeypatch.setattr(
        automation_module.time,
        "monotonic",
        lambda: clock["value"],
    )
    monkeypatch.setattr(
        runner,
        "_wait_interruptibly",
        lambda seconds: clock.__setitem__("value", clock["value"] + 31),
    )

    with pytest.raises(
        PaymentQrNotDetectedError,
        match="未检测到有效二维码",
    ):
        runner._capture_payment_qr(FakePage())
    assert state["full_page_calls"] == 0


def test_alipay_qr_capture_waits_for_placeholder_to_become_qr(monkeypatch):
    state = {"ready": False, "full_page_calls": 0, "waits": 0}

    class FakeCandidate:
        def is_visible(self):
            return True

        def bounding_box(self):
            return {"width": 180, "height": 180}

        def evaluate(self, _script):
            return {
                "marker": "qr-code",
                "context": "",
                "tag": "canvas",
                "descendants": 0,
            }

        def screenshot(self, **_kwargs):
            return b"QR" if state["ready"] else b"PLACEHOLDER"

    class FakeCandidateList:
        def count(self):
            return 1

        def nth(self, _index):
            return FakeCandidate()

    class FakePage:
        url = "https://pay.alipay.com/gateway"
        frames = ()

        def locator(self, selector):
            if selector == "body":
                return SimpleNamespace(inner_text=lambda **_kwargs: "支付宝")
            return FakeCandidateList()

        def screenshot(self, **_kwargs):
            state["full_page_calls"] += 1
            return b"FULL-PAYMENT-PAGE"

    clock = {"value": 0.0}
    monkeypatch.setattr(
        automation_module.time,
        "monotonic",
        lambda: clock["value"],
    )
    runner = CTExcelAutomation(
        AppConfig(),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    monkeypatch.setattr(
        runner,
        "_qr_visual_score",
        lambda image: 1.0 if image == b"QR" else 0.0,
    )

    def wait(seconds):
        state["waits"] += 1
        state["ready"] = True
        clock["value"] += seconds

    monkeypatch.setattr(runner, "_wait_interruptibly", wait)

    assert runner._capture_payment_qr(FakePage()) == b"QR"
    assert state["waits"] == 1
    assert state["full_page_calls"] == 0


def test_payment_timeout_deletes_qr_but_keeps_page_until_success(monkeypatch):
    clock = iter((0.0, 121.0))
    deleted = []
    state = {"success": False, "waits": 0}

    class FakePage:
        @property
        def url(self):
            return (
                "https://www.ctexcel.com/uk/buycard/buycardsucceed"
                if state["success"]
                else "https://www.ctexcel.com/uk/buycard/buycardWX"
            )

        def wait_for_timeout(self, _milliseconds):
            state["waits"] += 1
            state["success"] = True

    class FakeApi:
        def __init__(self):
            self.saved = None

        def save_payment_checkpoint(self, customer_id, **fields):
            self.saved = (customer_id, fields)
            return {"ok": True}

    runner = CTExcelAutomation(
        AppConfig(payment_timeout_seconds=120),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    monkeypatch.setattr(
        automation_module.time,
        "monotonic",
        lambda: next(clock),
    )
    monkeypatch.setattr(
        runner,
        "_delete_payment_qr",
        lambda reason: deleted.append(reason),
    )
    api = FakeApi()

    result = runner._wait_for_payment_success(
        FakePage(),
        api=api,
        customer_id=480,
        email="customer@example.test",
        pending_order={
            "order_number": "ORDERSUK2026073106180627794025",
            "transaction_amount": "1.00",
        },
    )

    assert result.order_number == "ORDERSUK2026073106180627794025"
    assert state["waits"] == 1
    assert deleted == ["支付等待超时", "支付成功"]


def test_browser_profile_is_isolated_and_automation_banner_is_removed():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "automation.py"
    ).read_text(encoding="utf-8")

    assert 'tempfile.mkdtemp(' in source
    assert '"--enable-automation"' in source
    assert '"--no-sandbox"' in source
    assert "--disable-blink-features=AutomationControlled" in source
    assert "Navigator.prototype" in source
    assert "cleanup_stale_browser_profiles(profile_root)" in source
    assert "remove_browser_profile(self.profile_dir)" in source
    assert "self.context.clear_cookies()" in source
    assert "localStorage.clear()" in source
    assert "requestfinished" in source
    assert "response.text()" in source
    assert "PURCHASE_LIMIT_MARKERS" in source
    assert "error-{stamp}-network.txt" in source


def test_browser_launch_failure_removes_partial_profile(monkeypatch, tmp_path):
    class FakeChromium:
        def launch_persistent_context(self, *_args, **_kwargs):
            raise RuntimeError("launch failed")

    class FakePlaywright:
        chromium = FakeChromium()

    class FakePlaywrightContext:
        def __enter__(self):
            return FakePlaywright()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        automation_module,
        "sync_playwright",
        lambda: FakePlaywrightContext(),
    )
    monkeypatch.setattr(automation_module, "app_config_dir", lambda: tmp_path)
    runner = CTExcelAutomation(
        AppConfig(browser_channel="chromium"),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )

    with pytest.raises(RuntimeError, match="launch failed"):
        runner._run_browser(
            object(),
            customer_id=1,
            email="customer@example.test",
            browser_proxy=None,
        )

    profile_root = tmp_path / "browser-runs"
    assert not list(profile_root.glob("order-*"))
    assert runner.profile_dir is None
    assert runner.context is None


def test_stale_browser_profile_cleanup_preserves_recent_runs(tmp_path: Path):
    old_profile = tmp_path / "order-old"
    recent_profile = tmp_path / "order-recent"
    unrelated = tmp_path / "manual-profile"
    old_profile.mkdir()
    recent_profile.mkdir()
    unrelated.mkdir()
    now = 1_800_000_000.0
    os.utime(old_profile, (now - 90_000, now - 90_000))
    os.utime(recent_profile, (now - 60, now - 60))
    os.utime(unrelated, (now - 90_000, now - 90_000))

    removed = cleanup_stale_browser_profiles(tmp_path, now=now)

    assert removed == 1
    assert not old_profile.exists()
    assert recent_profile.exists()
    assert unrelated.exists()


def test_diagnostic_response_excerpt_redacts_credentials():
    excerpt = diagnostic_response_excerpt(
        '{"purchase_limit":true,"AuthPwd":"secret-value",'
        '"message":"购买上限"}'
    )

    assert "purchase_limit" in excerpt
    assert "购买上限" in excerpt
    assert "secret-value" not in excerpt
    assert "<redacted>" in excerpt


def test_network_diagnostics_capture_purchase_limit_response_body():
    callbacks = {}

    class FakePage:
        def on(self, name, callback):
            callbacks[name] = callback

    class FakeResponse:
        status = 200

        def text(self):
            return (
                '{"message":"购买上限","AuthKey":"private-key"}'
            )

    class FakeRequest:
        url = "https://www.ctexcel.com/api/order/submit"
        method = "POST"
        failure = ""

        def response(self):
            return FakeResponse()

    runner = CTExcelAutomation(
        AppConfig(),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    runner._attach_page_diagnostics(FakePage())
    callbacks["requestfinished"](FakeRequest())

    assert len(runner.network_events) == 1
    assert "HTTP 200 POST /api/order/submit" in runner.network_events[0]
    assert "购买上限" in runner.network_events[0]
    assert "private-key" not in runner.network_events[0]
    assert "<redacted>" in runner.network_events[0]


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ("HTTP ERROR 407", "HTTP ERROR 407"),
        (
            "net::ERR_TUNNEL_CONNECTION_FAILED",
            "ERR_TUNNEL_CONNECTION_FAILED",
        ),
        (
            "net::ERR_PROXY_CONNECTION_FAILED",
            "ERR_PROXY_CONNECTION_FAILED",
        ),
        ("Proxy Authentication Required", "HTTP ERROR 407"),
    ],
)
def test_proxy_browser_errors_are_recognized(evidence, expected):
    assert expected in proxy_browser_error_reason(evidence)


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        ({"text": "", "visible_content": 0}, True),
        ({"text": "Loading...", "visible_content": 0}, True),
        ({"text": "加载中…", "visible_content": 0}, True),
        ({"text": "HTTP ERROR 407", "visible_content": 0}, False),
        ({"text": "", "visible_content": 1}, False),
    ],
)
def test_browser_startup_blank_snapshot_detection(snapshot, expected):
    assert browser_startup_snapshot_is_blank(snapshot) is expected


def test_tunnel_browser_starts_are_staggered_by_worker_slot():
    assert [tunnel_browser_start_delay(slot) for slot in range(1, 7)] == [
        0,
        5,
        10,
        15,
        20,
        20,
    ]


def test_registration_entry_blank_page_raises_retryable_error():
    class FakeBody:
        def inner_text(self, timeout):
            assert timeout == 1000
            return ""

    class FakePage:
        url = "https://www.ctexcel.com/freecard/home"

        def goto(self, url, *, wait_until, timeout):
            assert url == self.url
            assert wait_until == "domcontentloaded"
            assert timeout == 5000
            return SimpleNamespace(status=200)

        def wait_for_function(self, _script, *, timeout):
            assert 1 <= timeout <= 5000
            raise automation_module.PlaywrightTimeoutError("timeout")

        def evaluate(self, _script):
            return {
                "text": "",
                "visible_content": 0,
                "ready_state": "complete",
                "title": "",
                "url": self.url,
            }

        def title(self):
            return ""

        def locator(self, selector):
            assert selector == "body"
            return FakeBody()

    runner = CTExcelAutomation(
        AppConfig(page_timeout_ms=5000),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )

    with pytest.raises(RetryableBlankPageError, match="仍是一片空白"):
        runner._open_registration_entry(
            FakePage(),
            "https://www.ctexcel.com/freecard/home",
            label="活动页",
            ready_script="() => false",
        )


def test_registration_entry_nonblank_unexpected_page_is_diagnostic_error():
    class FakeBody:
        def inner_text(self, timeout):
            return "网站维护通知"

    class FakePage:
        url = "https://www.ctexcel.com/freecard/home"

        def goto(self, *_args, **_kwargs):
            return SimpleNamespace(status=200)

        def wait_for_function(self, _script, *, timeout):
            raise automation_module.PlaywrightTimeoutError("timeout")

        def evaluate(self, _script):
            return {
                "text": "网站维护通知",
                "visible_content": 0,
                "ready_state": "complete",
                "title": "维护中",
                "url": self.url,
            }

        def title(self):
            return "维护中"

        def locator(self, _selector):
            return FakeBody()

    runner = CTExcelAutomation(
        AppConfig(page_timeout_ms=5000),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )

    with pytest.raises(AutomationError, match="当前页面非空白"):
        runner._open_registration_entry(
            FakePage(),
            "https://www.ctexcel.com/freecard/home",
            label="活动页",
            ready_script="() => false",
        )


def test_payment_popup_is_selected_instead_of_waiting_on_confirmation_page():
    class FakePage:
        def __init__(self, url):
            self.url = url

        def is_closed(self):
            return False

    source = FakePage(
        "https://www.ctexcel.com/freecard/activityPageconfirm"
    )
    popup = FakePage("https://www.ctexcel.com/freecard/buycardWX")
    messages = []
    runner = CTExcelAutomation(
        AppConfig(page_timeout_ms=5000),
        log=messages.append,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    runner.context = SimpleNamespace(pages=[source, popup])

    selected = runner._wait_for_wechat_payment_page(source)

    assert selected is popup
    assert any("新窗口" in message for message in messages)


def test_payment_terms_wait_for_vue_binding_before_next_click(monkeypatch):
    state = {
        "checked": False,
        "vue_bound": False,
        "dialog_visible": True,
        "clicks": 0,
    }

    class FakeCheckbox:
        def count(self):
            return 1

        def evaluate(self, script):
            if "root.click" in script:
                state["checked"] = True
                return True
            if "requestAnimationFrame" in script:
                state["vue_bound"] = state["checked"]
                return state["vue_bound"]
            raise AssertionError(script)

        def is_checked(self):
            return state["checked"]

    class FakeSubmit:
        def click(self, **_kwargs):
            state["clicks"] += 1
            if state["vue_bound"]:
                state["dialog_visible"] = False

    class FakeDialog:
        checkbox = FakeCheckbox()
        submit = FakeSubmit()

        def get_by_role(self, role, **_kwargs):
            return self.checkbox if role == "checkbox" else self.submit

        def is_visible(self):
            return state["dialog_visible"]

    class FakePage:
        url = "https://example.test/freecard/activityPageconfirm"

    messages = []
    runner = CTExcelAutomation(
        AppConfig(),
        log=messages.append,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    monkeypatch.setattr(
        runner,
        "_visible_locator",
        lambda locator, _label: locator,
    )

    runner._submit_payment_terms(FakePage(), FakeDialog())

    assert state == {
        "checked": True,
        "vue_bound": True,
        "dialog_visible": False,
        "clicks": 1,
    }
    assert any("完成页面状态同步" in item for item in messages)
    assert any("仅提交一次" in item for item in messages)


def test_alipay_payment_tile_selects_image_only_gateway(monkeypatch):
    class FakeCollection:
        def __init__(self, items):
            self.items = list(items)

        def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

        def filter(self, *, has_text):
            return FakeCollection(
                [item for item in self.items if has_text in item.text]
            )

    class FakeTile:
        def __init__(self, text, *, has_image):
            self.text = text
            self.has_image = has_image
            self.active = False

        def is_visible(self):
            return True

        def inner_text(self):
            return self.text

        def locator(self, selector):
            assert selector == "img"
            return FakeCollection([object()] if self.has_image else [])

        def click(self):
            self.active = True

        def evaluate(self, _script):
            return self.active

    class FakeRadio:
        checked = False

        def is_checked(self):
            return self.checked

        def check(self):
            self.checked = True

    class FakePage:
        def __init__(self):
            self.radio = FakeRadio()
            self.tiles = FakeCollection(
                [
                    FakeTile("Paypal", has_image=True),
                    FakeTile("", has_image=True),
                    FakeTile("微信", has_image=True),
                ]
            )

        def get_by_role(self, role, **_kwargs):
            assert role == "radio"
            return self.radio

        def locator(self, selector):
            assert selector == ".pay-method-type > div"
            return self.tiles

        def get_by_text(self, _text, **_kwargs):
            return FakeCollection([])

    messages = []
    runner = CTExcelAutomation(
        AppConfig(payment_method=PAYMENT_METHOD_ALIPAY),
        log=messages.append,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    monkeypatch.setattr(runner, "_wait_for_page_ready", lambda *_a, **_k: None)

    page = FakePage()
    runner._select_payment_method(page)

    assert page.radio.checked is True
    assert page.tiles.nth(1).active is True
    assert any("银行卡/支付宝支付网关" in item for item in messages)


def test_default_payment_method_remains_wechat_for_existing_configs():
    assert AppConfig().payment_method == PAYMENT_METHOD_WECHAT


def test_alipay_gateway_wait_accepts_new_checkout_iframe(monkeypatch):
    state = {"iframe_reads": 0}

    class FakeFrame:
        url = "https://na.gateway.spring.citi.com/checkout/widget"

    class FakeCount:
        def __init__(self, value):
            self.value = value

        def count(self):
            return self.value

    class FakeBody:
        def inner_text(self, **_kwargs):
            return "订单确认"

    class FakePage:
        url = "https://www.ctexcel.com/uk/buycard/buycardlist"
        frames = [FakeFrame()]
        main_frame = None

        def is_closed(self):
            return False

        def locator(self, selector):
            if selector == "iframe":
                state["iframe_reads"] += 1
                return FakeCount(0 if state["iframe_reads"] == 1 else 1)
            if selector == "body":
                return FakeBody()
            if "checkout" in selector or "payment" in selector:
                return FakeCount(0)
            raise AssertionError(selector)

    messages = []
    runner = CTExcelAutomation(
        AppConfig(payment_method=PAYMENT_METHOD_ALIPAY),
        log=messages.append,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    runner.context = SimpleNamespace(pages=[])

    selected = runner._wait_for_alipay_payment_page(FakePage())

    assert selected.url.endswith("/buycardlist")
    assert any("支付网关已在" in item for item in messages)


def test_alipay_gateway_selects_option_and_clicks_pay_before_qr(monkeypatch):
    calls = []

    class FakePage:
        url = "https://na.gateway.spring.citi.com/checkout/pay/SESSION"

        def is_closed(self):
            return False

        def evaluate(self, script, *args):
            calls.append((script, args))
            if "scanqrcode" in script:
                return len(calls) >= 4
            if "target.click()" in script:
                return {"found": True, "clicked": True}
            if "data-ctexcel-alipay-pay" in script:
                return {"found": True, "enabled": True}
            raise AssertionError("unexpected gateway script")

    messages = []
    runner = CTExcelAutomation(
        AppConfig(payment_method=PAYMENT_METHOD_ALIPAY),
        log=messages.append,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )

    selected = runner._complete_alipay_gateway_selection(
        FakePage(),
        expected_amount="5.95",
    )

    assert selected.url.endswith("/SESSION")
    assert len(calls) == 4
    assert calls[1][1] == ()
    assert calls[2][1] == ("5.95",)
    assert any("已点击支付按钮" in item for item in messages)


def test_alipay_gateway_controls_are_found_inside_cross_origin_frame():
    state = {
        "frame_selected": False,
        "frame_clicked": False,
        "outer_selected": False,
        "outer_clicked": False,
    }

    class FakeFrame:
        url = "https://na.gateway.spring.citi.com/checkout/widget"

        def evaluate(self, script, *_args):
            if "scanqrcode" in script:
                return state["frame_clicked"]
            if "target.click()" in script:
                state["frame_selected"] = True
                return {"found": True, "clicked": True}
            if "data-ctexcel-alipay-pay" in script:
                state["frame_clicked"] = True
                return {"found": True, "enabled": True}
            raise AssertionError("unexpected frame script")

    class FakePage:
        url = "https://na.gateway.spring.citi.com/checkout/pay/SESSION"
        frames = [FakeFrame()]
        main_frame = None

        def is_closed(self):
            return False

        def evaluate(self, script, *_args):
            if "scanqrcode" in script:
                return False
            if "target.click()" in script:
                state["outer_selected"] = True
                return {"found": True, "clicked": True}
            if "data-ctexcel-alipay-pay" in script:
                state["outer_clicked"] = True
                return {"found": True, "enabled": True}
            return {"found": False, "enabled": False}

    runner = CTExcelAutomation(
        AppConfig(payment_method=PAYMENT_METHOD_ALIPAY),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )

    selected = runner._complete_alipay_gateway_selection(
        FakePage(),
        expected_amount="11.00",
    )

    assert isinstance(selected, FakePage)
    assert state == {
        "frame_selected": True,
        "frame_clicked": True,
        "outer_selected": False,
        "outer_clicked": False,
    }


def test_alipay_gateway_pay_script_supports_input_cta_and_frame_amount_scope():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "automation.py"
    ).read_text(encoding="utf-8")

    assert "input[type=\"button\"]" in source
    assert "element?.value" in source
    assert "documentAmountMatches" in source
    assert "data-ctexcel-alipay-pay" in source
    assert "系统将跳转到支付宝网站" in source


def test_alipay_qr_wait_accepts_alipay_navigation_inside_frame():
    class FakeFrame:
        url = "https://global.alipay.com/qr/SESSION"

    class FakePage:
        url = "https://na.gateway.spring.citi.com/checkout/pay/SESSION"
        frames = [FakeFrame()]
        main_frame = None

        def is_closed(self):
            return False

    runner = CTExcelAutomation(
        AppConfig(payment_method=PAYMENT_METHOD_ALIPAY),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )

    assert runner._wait_for_alipay_qr_page(FakePage()).url.endswith("SESSION")


def test_alipay_qr_wait_accepts_ppro_redirect_qr_page():
    class FakePage:
        url = "https://redirect-euw1.ppro.com/payment/SESSION"

        def is_closed(self):
            return False

        def evaluate(self, script, *_args):
            if "scanqrcode" in script:
                return True
            raise AssertionError("only QR readiness should be evaluated")

    runner = CTExcelAutomation(
        AppConfig(payment_method=PAYMENT_METHOD_ALIPAY),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )

    assert runner._wait_for_alipay_qr_page(FakePage()).url.endswith("SESSION")


def test_alipay_direct_qr_url_skips_gateway_selection():
    class FakePage:
        url = "https://global.alipay.com/qr/SESSION"

        def evaluate(self, _script, *_args):
            raise AssertionError("direct Alipay QR should not inspect gateway")

    runner = CTExcelAutomation(
        AppConfig(payment_method=PAYMENT_METHOD_ALIPAY),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )

    page = FakePage()
    assert runner._complete_alipay_gateway_selection(
        page,
        expected_amount="5.95",
    ) is page


def test_payment_terms_never_retries_a_non_idempotent_submit(monkeypatch):
    state = {"checked": False, "dialog_visible": True, "clicks": 0}
    clock = {"value": 0.0}

    class FakeCheckbox:
        def count(self):
            return 1

        def evaluate(self, script):
            if "root.click" in script:
                state["checked"] = True
                return True
            if "requestAnimationFrame" in script:
                return state["checked"]
            raise AssertionError(script)

        def is_checked(self):
            return state["checked"]

    class FakeSubmit:
        def click(self, **_kwargs):
            state["clicks"] += 1

    class FakeDialog:
        checkbox = FakeCheckbox()
        submit = FakeSubmit()

        def get_by_role(self, role, **_kwargs):
            return self.checkbox if role == "checkbox" else self.submit

        def is_visible(self):
            return state["dialog_visible"]

    class FakePage:
        url = "https://example.test/freecard/activityPageconfirm"

    messages = []
    runner = CTExcelAutomation(
        AppConfig(),
        log=messages.append,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    monkeypatch.setattr(
        runner,
        "_visible_locator",
        lambda locator, _label: locator,
    )

    runner._submit_payment_terms(FakePage(), FakeDialog())

    assert state["clicks"] == 1
    assert state["dialog_visible"] is True
    assert any("避免重复创建订单" in item for item in messages)


def test_same_url_wechat_qr_content_is_accepted(monkeypatch):
    body_text = (
        "请使用微信扫描二维码以完成支付 ¥9.11(1GBP) "
        "订单号码：ORDERSUK202608010000000001"
    )
    assert payment_page_content_is_ready(body_text)

    class FakeBody:
        def inner_text(self, **_kwargs):
            return body_text

    class FakePage:
        url = "https://example.test/freecard/activityPageconfirm"

        def is_closed(self):
            return False

        def locator(self, selector):
            assert selector == "body"
            return FakeBody()

    page = FakePage()
    messages = []
    runner = CTExcelAutomation(
        AppConfig(),
        log=messages.append,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    runner.context = SimpleNamespace(pages=[page])

    selected = runner._wait_for_wechat_payment_page(page)

    assert selected is page
    assert any("无需等待网址跳转" in item for item in messages)


@pytest.mark.parametrize(
    "body_text",
    [
        "请使用微信扫描二维码 订单号：ORDER202608010000000001",
        "Scan the QR code with WeChat Order: ORDERSUK202608010000000001",
    ],
)
def test_payment_page_readiness_accepts_order_label_variants(body_text):
    assert payment_page_content_is_ready(body_text)
    assert not payment_page_content_is_ready(
        "订单号：ORDER202608010000000001 scanqrcode"
    )


def test_proxy_browser_error_skips_manual_hold_but_normal_error_preserves(
    monkeypatch,
):
    class FakeBody:
        def __init__(self, text):
            self.text = text

        def inner_text(self, timeout):
            assert timeout == 1000
            return self.text

    class FakePage:
        def __init__(self, text):
            self.text = text

        def title(self):
            return "该网页无法正常运作"

        def locator(self, selector):
            assert selector == "body"
            return FakeBody(self.text)

    preserved = []
    runner = CTExcelAutomation(
        AppConfig(),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    monkeypatch.setattr(
        runner,
        "_preserve_error_page",
        lambda page, exc: preserved.append((page, exc)),
    )

    with pytest.raises(RetryableProxyBrowserError, match="407"):
        runner._raise_if_proxy_error_page(
            FakePage(""),
            SimpleNamespace(status=407),
        )

    with pytest.raises(RetryableProxyBrowserError, match="407"):
        runner._handle_browser_failure(
            FakePage("HTTP ERROR 407"),
            RuntimeError("navigation failed"),
            browser_proxy={"server": "http://proxy.example.test:10001"},
        )

    assert preserved == []

    normal_error = RuntimeError("没有定位到页面按钮")
    normal_page = FakePage("正常业务页面")
    with pytest.raises(RuntimeError, match="没有定位到页面按钮"):
        runner._handle_browser_failure(
            normal_page,
            normal_error,
            browser_proxy={"server": "http://proxy.example.test:10001"},
        )

    assert preserved == [(normal_page, normal_error)]


def test_pre_payment_timeout_restarts_but_qr_wait_is_exempt(monkeypatch):
    class FakeBody:
        def inner_text(self, timeout):
            return "正常业务页面"

    class FakePage:
        def is_closed(self):
            return False

        def title(self):
            return "申请页"

        def locator(self, _selector):
            return FakeBody()

    runner = CTExcelAutomation(
        AppConfig(),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    preserved = []
    monkeypatch.setattr(
        runner,
        "_preserve_error_page",
        lambda page, exc: preserved.append((page, exc)),
    )
    timeout = automation_module.PlaywrightTimeoutError("timeout")

    with pytest.raises(RetryableStalledPageError, match="20 秒"):
        runner._handle_browser_failure(
            FakePage(),
            timeout,
            browser_proxy=None,
        )
    assert preserved == []

    runner.payment_qr_reached = True
    with pytest.raises(automation_module.PlaywrightTimeoutError):
        runner._handle_browser_failure(
            FakePage(),
            timeout,
            browser_proxy=None,
        )
    assert len(preserved) == 1


@pytest.mark.parametrize(
    "retry_error",
    [RetryableProxyBrowserError, RetryableBlankPageError],
)
def test_api_browser_error_reopens_with_same_customer_and_fresh_proxy(
    monkeypatch,
    retry_error,
):
    prepare_calls = []
    browser_attempts = []
    customer_creations = []
    customer_callbacks = []
    route_closes = []
    logs = []

    def fake_prepare_proxy(_config, *, resolved_proxy=None):
        prepare_calls.append(resolved_proxy)
        proxy = resolved_proxy or {
            "server": f"http://203.0.113.{len(prepare_calls)}:10001"
        }
        return SimpleNamespace(
            playwright_proxy=proxy,
            public_ip="",
            public_ip_error="",
        )

    class FakeRoute:
        bridge = None

        def __init__(self, proxy):
            self.proxy = dict(proxy) if proxy else None

        def close(self):
            route_closes.append(self.proxy)

    class FakeAdminApi:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def connect(self):
            pass

        def create_ctexcel_customer(self, **_kwargs):
            customer_creations.append(1)
            return {
                "customer_id": 407,
                "email": "same-customer@example.test",
                "reused": False,
            }

    monkeypatch.setattr(automation_module, "prepare_proxy", fake_prepare_proxy)
    monkeypatch.setattr(
        automation_module,
        "browser_compatible_proxy",
        lambda proxy: FakeRoute(proxy),
    )
    monkeypatch.setattr(automation_module, "AdminApi", FakeAdminApi)

    initial_proxy = {"server": "http://198.51.100.10:10001"}
    runner = CTExcelAutomation(
        AppConfig(
            proxy=ProxyConfig(
                mode="api",
                api_url="https://share.proxy.qg.net/get?num=1",
            ),
            registration=RegistrationDefaults(
                last_name="测试姓",
                first_name="测试名",
                contact_phone="13800000000",
                chinese_address="测试地址",
            ),
        ),
        log=logs.append,
        stage=lambda _stage: None,
        customer_created=customer_callbacks.append,
        reuse_pending_customer=False,
        proxy_override=initial_proxy,
    )
    monkeypatch.setattr(runner, "_wait_interruptibly", lambda _seconds: None)

    def fake_run_browser(
        _api,
        customer_id,
        email,
        *,
        browser_proxy,
        synchronize_start,
    ):
        browser_attempts.append(
            (customer_id, email, browser_proxy, synchronize_start)
        )
        if len(browser_attempts) < 3:
            raise retry_error("浏览器入口需要重试")
        return AutomationResult(customer_id=customer_id, email=email)

    monkeypatch.setattr(runner, "_run_browser", fake_run_browser)

    result = runner.run()

    assert result.customer_id == 407
    assert customer_creations == [1]
    assert len(customer_callbacks) == 1
    assert prepare_calls == [initial_proxy, None, None]
    assert [item[0] for item in browser_attempts] == [407, 407, 407]
    assert [item[1] for item in browser_attempts] == [
        "same-customer@example.test",
    ] * 3
    assert [item[2]["server"] for item in browser_attempts] == [
        "http://198.51.100.10:10001",
        "http://203.0.113.2:10001",
        "http://203.0.113.3:10001",
    ]
    assert [item[3] for item in browser_attempts] == [True, False, False]
    assert len(route_closes) == 3
    assert any("第 2 / 3 次" in item for item in logs)
    assert any("第 3 / 3 次" in item for item in logs)


def test_proxy_browser_retry_stops_after_three_attempts(monkeypatch):
    prepare_calls = []
    customer_creations = []
    browser_attempts = []
    releases = []

    def fake_prepare_proxy(_config, *, resolved_proxy=None):
        prepare_calls.append(resolved_proxy)
        return SimpleNamespace(
            playwright_proxy={
                "server": f"http://203.0.113.{len(prepare_calls)}:10001"
            },
            public_ip="",
            public_ip_error="",
        )

    class FakeRoute:
        bridge = None

        def __init__(self, proxy):
            self.proxy = proxy

        def close(self):
            pass

    class FakeAdminApi:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def connect(self):
            pass

        def create_ctexcel_customer(self, **_kwargs):
            customer_creations.append(1)
            return {
                "customer_id": 408,
                "email": "retry-limit@example.test",
                "reused": False,
            }

        def release_ctexcel_customer(self, customer_id, *, request_key):
            releases.append((customer_id, request_key))
            return True

    monkeypatch.setattr(automation_module, "prepare_proxy", fake_prepare_proxy)
    monkeypatch.setattr(
        automation_module,
        "browser_compatible_proxy",
        lambda proxy: FakeRoute(proxy),
    )
    monkeypatch.setattr(automation_module, "AdminApi", FakeAdminApi)

    runner = CTExcelAutomation(
        AppConfig(
            proxy=ProxyConfig(mode="api"),
            registration=RegistrationDefaults(
                last_name="测试姓",
                first_name="测试名",
                contact_phone="13800000000",
                chinese_address="测试地址",
            ),
        ),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
        reuse_pending_customer=False,
    )
    monkeypatch.setattr(runner, "_wait_interruptibly", lambda _seconds: None)

    def always_fail(*_args, **_kwargs):
        browser_attempts.append(1)
        raise RetryableProxyBrowserError(
            "ERR_TUNNEL_CONNECTION_FAILED（代理隧道建立失败）"
        )

    monkeypatch.setattr(runner, "_run_browser", always_fail)

    with pytest.raises(AutomationError, match="连续 3 次"):
        runner.run()

    assert customer_creations == [1]
    assert len(browser_attempts) == 3
    assert len(prepare_calls) == 3
    assert releases == [(408, runner.request_key)]


def test_application_flow_has_no_phone_capture_or_gate():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "automation.py"
    ).read_text(encoding="utf-8")

    assert "CAPTURE_CTEXCEL_ORDER_RESPONSES_SCRIPT" not in source
    assert "XMLHttpRequest.prototype.open" not in source
    assert "window.fetch = async" not in source
    assert "CTEXCEL_ORDER_CAPTURE:" not in source
    assert "_wait_for_confirmed_phone" not in source
    assert "captured_phone_number" not in source
    assert "getStuOrderCardDetail" not in source
    assert "客户端不再读取手机号，本单立即完成" in source
    assert "订单确认接口没有返回手机号，已停止进入支付" not in source


def test_authenticated_socks_bridge_is_preflighted_before_customer_creation(
    monkeypatch,
):
    events = []

    class FakeRoute:
        proxy = {"server": "socks5://127.0.0.1:32123"}
        bridge = object()

        def close(self):
            events.append("closed")

    monkeypatch.setattr(
        automation_module,
        "prepare_proxy",
        lambda *_args, **_kwargs: SimpleNamespace(
            playwright_proxy={
                "server": "socks5://proxy.example.test:3010",
                "username": "user",
                "password": "pass",
            },
            public_ip="",
            public_ip_error="",
        ),
    )
    monkeypatch.setattr(
        automation_module,
        "browser_compatible_proxy",
        lambda _proxy: FakeRoute(),
    )

    def fail_bridge_probe(_proxy):
        events.append("bridge-probe")
        raise ProxyError("本机桥接连接失败")

    monkeypatch.setattr(
        automation_module,
        "probe_proxy_endpoint",
        fail_bridge_probe,
    )

    class UnexpectedAdminApi:
        def __init__(self, *_args, **_kwargs):
            events.append("customer-api")
            raise AssertionError("代理预检失败时不应连接客户 API")

    monkeypatch.setattr(
        automation_module,
        "AdminApi",
        UnexpectedAdminApi,
    )
    config = AppConfig(
        proxy=ProxyConfig(mode="custom"),
        registration=RegistrationDefaults(
            last_name="朱",
            first_name="先生",
            contact_phone="18170908787",
            chinese_address="测试地址",
        ),
    )
    runner = CTExcelAutomation(
        config,
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )

    with pytest.raises(AutomationError, match="本机桥接连接失败"):
        runner.run()

    assert events == ["bridge-probe", "closed"]


def test_browser_launch_skips_external_ip_detection():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "automation.py"
    ).read_text(encoding="utf-8")

    assert "api.ipify.org" not in source
    assert "1.1.1.1/cdn-cgi/trace" not in source
    assert "跳过第三方 IP 检测并直接进入注册" in source


def test_stopping_a_worker_releases_the_initial_browser_barrier():
    barrier = threading.Barrier(2)
    runner = CTExcelAutomation(
        AppConfig(),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
        browser_start_barrier=barrier,
    )

    runner.stop()

    assert barrier.broken is True


def test_sim_configuration_tracks_current_page_dom_and_preserves_errors():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "automation.py"
    ).read_text(encoding="utf-8")

    assert '"实体SIM卡",\n            exact=False' in source
    assert "全部拒绝" in source
    assert ".uc-deny-button" in source
    assert "COOKIE_CONSENT_WATCHER_SCRIPT" in source
    assert "__ctexcelDismissCookieConsent" in source
    assert "已自动关闭隐私设置" in source
    assert "'全部接受'" in source
    assert 'page.locator(".el-switch")' in source
    assert "'.el-loading-mask'" in source
    assert "连续 20 秒没有 URL、Loading 或 DOM 变化" in source
    assert "def _click_button_and_wait_for_page" in source
    assert '"networkidle"' not in source
    configure_start = source.index("def _configure_sim")
    switch_click = source.index("switch.click()", configure_start)
    wait_before_switch = source.index(
        'self._wait_for_page_ready(page, "自动续订开关")',
        configure_start,
    )
    assert wait_before_switch < switch_click
    assert (
        'self._wait_for_page_ready(page, "关闭自动续订")'
        in source[switch_click:]
    )
    assert "错误现场已保留" in source
    assert "error_browser_hold_seconds" in source
    assert "def _start_freecard_application" in source
    assert "FREECARD_APPLICATION_URL" in source
    assert "先预存£1领卡" in source
    assert "activityPagefillInfos" in source
    assert "activityPageconfirm" in source
    assert "freecard/buycardwx" in source
    assert "save_payment_checkpoint" in source
    assert "allow_new_after_checkpoint" in source
    assert "连续申请模式：本单已完成" in source


def test_freecard_route_is_the_new_default():
    config = AppConfig()

    assert config.purchase_route == PURCHASE_ROUTE_FREECARD
    assert config.registration.freecard_referrer == "447942946765"


def test_registration_ranges_increment_phone_and_append_address_suffix():
    defaults = RegistrationDefaults(
        contact_phone="13800000000",
        contact_phone_end="13800000999",
        chinese_address="测试省测试市测试区测试路测试驿站1111",
        address_suffix_start=1,
        address_suffix_end=1000,
    )

    assert registration_values_for_ordinal(defaults, 1) == (
        "13800000000",
        defaults.chinese_address + "1",
    )
    assert registration_values_for_ordinal(defaults, 2) == (
        "13800000001",
        defaults.chinese_address + "2",
    )
    assert registration_values_for_ordinal(defaults, 1000) == (
        "13800000999",
        defaults.chinese_address + "1000",
    )


def test_address_suffix_is_appended_after_smart_recognition():
    assert append_address_suffix("测试路88号收货点", 200) == (
        "测试路88号收货点200"
    )


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("江西省南昌市测试区测试路1号", "江西省"),
        ("北京市朝阳区测试路1号", "北京市"),
        ("内蒙古自治区呼和浩特市测试路1号", "内蒙古自治区"),
    ],
)
def test_address_region_token_tracks_configured_province(address, expected):
    assert address_region_token(address) == expected


def test_registration_ranges_fail_before_reusing_an_exhausted_value():
    defaults = RegistrationDefaults(
        contact_phone="13800000000",
        contact_phone_end="13800000001",
        chinese_address="固定地址1111",
        address_suffix_start=1,
        address_suffix_end=2,
    )

    with pytest.raises(AutomationError, match="联系电话区间不足"):
        registration_values_for_ordinal(defaults, 3)


def test_empty_phone_end_keeps_legacy_fixed_phone_but_numbers_addresses():
    defaults = RegistrationDefaults(
        contact_phone="13800000000",
        chinese_address="固定地址1111",
        address_suffix_start=1,
        address_suffix_end=1000,
    )

    assert registration_values_for_ordinal(defaults, 23) == (
        "13800000000",
        "固定地址111123",
    )


def test_continuous_runner_waits_for_each_completed_item_before_next():
    started = []
    completed = []
    created_count = 0

    class FakeAutomation:
        def __init__(self, _config, **_callbacks):
            nonlocal created_count
            created_count += 1
            self.ordinal = created_count

        def run(self):
            return AutomationResult(
                customer_id=self.ordinal,
                email=f"customer-{self.ordinal}@example.test",
                order_number=f"ORDERSUK20260731{self.ordinal:012d}",
                transaction_amount="1.00",
            )

        def stop(self):
            pass

    config = AppConfig(
        continuous_enabled=True,
        continuous_count=3,
        continuous_interval_seconds=0,
    )
    runner = CTExcelBatchAutomation(
        config,
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
        item_started=lambda ordinal, total: started.append(
            (ordinal, total)
        ),
        item_completed=lambda result, ordinal, total: completed.append(
            (result.customer_id, ordinal, total)
        ),
        automation_factory=FakeAutomation,
    )

    result = runner.run()

    assert application_target(config) == 3
    assert started == [(1, 3), (2, 3), (3, 3)]
    assert completed == [(1, 1, 3), (2, 2, 3), (3, 3, 3)]
    assert result.completed_count == 3
    assert result.total_count == 3
    assert result.last_result.customer_id == 3


def test_continuous_runner_resumes_after_completed_items():
    started = []

    class FakeAutomation:
        def __init__(self, _config, **_callbacks):
            pass

        def run(self):
            return AutomationResult(
                customer_id=9,
                email="resume@example.test",
            )

        def stop(self):
            pass

    runner = CTExcelBatchAutomation(
        AppConfig(
            continuous_enabled=True,
            continuous_count=3,
            continuous_interval_seconds=0,
        ),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
        item_started=lambda ordinal, total: started.append(
            (ordinal, total)
        ),
        item_completed=lambda *_args: None,
        completed_before=2,
        automation_factory=FakeAutomation,
    )

    result = runner.run()

    assert started == [(3, 3)]
    assert result.completed_count == 3


def test_continuous_runner_supports_ten_safe_parallel_workers():
    state_lock = threading.Lock()
    active = 0
    max_active = 0
    created = 0
    request_keys = []
    reuse_flags = []
    worker_slots = []
    browser_start_barriers = []
    completed_ordinals = []

    class FakeAutomation:
        def __init__(
            self,
            _config,
            *,
            request_key,
            reuse_pending_customer,
            worker_slot,
            browser_start_barrier,
            **_callbacks,
        ):
            nonlocal created
            with state_lock:
                created += 1
                self.customer_id = created
                request_keys.append(request_key)
                reuse_flags.append(reuse_pending_customer)
                worker_slots.append(worker_slot)
                browser_start_barriers.append(browser_start_barrier)

        def run(self):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.04)
            with state_lock:
                active -= 1
            return AutomationResult(
                customer_id=self.customer_id,
                email=f"parallel-{self.customer_id}@example.test",
            )

        def stop(self):
            pass

    runner = CTExcelBatchAutomation(
        AppConfig(
            continuous_enabled=True,
            continuous_count=12,
            continuous_workers=10,
            continuous_interval_seconds=0,
        ),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
        item_started=lambda *_args: None,
        item_completed=lambda result, _completed, _total: (
            completed_ordinals.append(result.batch_ordinal)
        ),
        automation_factory=FakeAutomation,
    )

    result = runner.run()

    assert result.completed_count == 12
    assert max_active == 10
    assert len(set(request_keys)) == 12
    assert reuse_flags.count(True) == 1
    assert set(worker_slots) == set(range(1, 11))
    first_wave_barriers = [
        barrier for barrier in browser_start_barriers if barrier is not None
    ]
    assert len(first_wave_barriers) == 10
    assert len({id(barrier) for barrier in first_wave_barriers}) == 1
    assert browser_start_barriers.count(None) == 2
    assert sorted(completed_ordinals) == list(range(1, 13))


def test_parallel_completion_progress_waits_for_contiguous_ordinals():
    completed_progress = []

    class FakeAutomation:
        def __init__(self, _config, *, batch_ordinal, **_kwargs):
            self.ordinal = batch_ordinal

        def run(self):
            time.sleep({1: 0.06, 2: 0.03, 3: 0.0}[self.ordinal])
            return AutomationResult(
                customer_id=self.ordinal,
                email=f"parallel-{self.ordinal}@example.test",
            )

        def stop(self):
            pass

    runner = CTExcelBatchAutomation(
        AppConfig(
            continuous_enabled=True,
            continuous_count=3,
            continuous_workers=3,
            continuous_interval_seconds=0,
        ),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
        item_started=lambda *_args: None,
        item_completed=lambda result, progress, _total: completed_progress.append(
            (result.customer_id, progress)
        ),
        automation_factory=FakeAutomation,
    )

    result = runner.run()

    assert result.completed_count == 3
    assert completed_progress[0] == (3, 0)
    assert completed_progress[-1] == (1, 3)


def test_batch_stop_during_session_creation_stops_without_running_session():
    factory_ready = threading.Event()
    release_factory = threading.Event()
    events = []

    class FakeAutomation:
        def __init__(self, _config, **_kwargs):
            factory_ready.set()
            release_factory.wait(2)

        def run(self):
            events.append("run")
            return AutomationResult(customer_id=1, email="stop@example.test")

        def stop(self):
            events.append("stop")

    runner = CTExcelBatchAutomation(
        AppConfig(),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
        item_started=lambda *_args: None,
        item_completed=lambda *_args: None,
        automation_factory=FakeAutomation,
    )
    outcome = []

    def invoke():
        try:
            runner._run_item(
                ordinal=1,
                total=1,
                worker_slot=1,
                reuse_pending_customer=True,
            )
        except Exception as exc:  # noqa: BLE001 - test boundary
            outcome.append(exc)

    thread = threading.Thread(target=invoke)
    thread.start()
    assert factory_ready.wait(2)
    runner.stop()
    release_factory.set()
    thread.join(2)

    assert outcome and isinstance(outcome[0], AutomationError)
    assert "run" not in events
    assert events == ["stop"]


def test_batch_assigns_distinct_unpaid_customers_before_creating_new(
    monkeypatch,
):
    sync_calls = []

    class FakeAdminApi:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def connect(self):
            return {"ok": True, "api_version": 8}

        def pending_customers(self):
            return [
                {
                    "customer_id": 4,
                    "email": "pending-4@example.test",
                    "order_number": "ORDER-PAID",
                    "payment_succeeded_at": "2026-08-01T00:10:00Z",
                    "registration_confirmed_at": None,
                },
                {
                    "customer_id": 3,
                    "email": "pending-3@example.test",
                    "order_number": "ORDER-UNPAID",
                    "payment_succeeded_at": None,
                    "registration_confirmed_at": None,
                },
                {
                    "customer_id": 2,
                    "email": "pending-2@example.test",
                    "order_number": "ORDER-CONFIRMED",
                    "payment_succeeded_at": None,
                    "registration_confirmed_at": None,
                },
                {
                    "customer_id": 1,
                    "email": "pending-1@example.test",
                    "order_number": None,
                    "payment_succeeded_at": None,
                    "registration_confirmed_at": None,
                },
            ]

        def sync_order_info(self, customer_id):
            sync_calls.append(customer_id)
            return {
                "registration_confirmed": customer_id == 2,
            }

    monkeypatch.setattr(automation_module, "AdminApi", FakeAdminApi)
    messages = []
    runner = CTExcelBatchAutomation(
        AppConfig(continuous_enabled=True, continuous_count=3),
        log=messages.append,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
        item_started=lambda *_args: None,
        item_completed=lambda *_args: None,
    )

    runner._prepare_resume_customer_ids(first_ordinal=1, total=3)

    assert runner.resume_assignment_supported is True
    assert runner.resume_customer_ids_by_ordinal == {1: 1, 2: 3}
    assert sync_calls == [2, 3]
    assert any("不同的未成功付款客户" in item for item in messages)


def test_resume_customer_is_revalidated_by_server_create_endpoint(
    monkeypatch,
):
    create_calls = []

    class FakeAdminApi:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def connect(self):
            return {"ok": True, "api_version": 7}

        def create_ctexcel_customer(self, **kwargs):
            create_calls.append(kwargs)
            return {
                "customer_id": kwargs["resume_customer_id"],
                "email": "pending-701@example.test",
                "reused": True,
            }

    class FakeRoute:
        proxy = None

        def close(self):
            pass

    monkeypatch.setattr(automation_module, "AdminApi", FakeAdminApi)
    customer_events = []
    runner = CTExcelAutomation(
        AppConfig(
            registration=RegistrationDefaults(
                last_name="朱",
                first_name="先生",
                contact_phone="18170908000",
                chinese_address="测试省测试市测试区测试地址",
            )
        ),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=customer_events.append,
        resume_customer_id=701,
    )
    monkeypatch.setattr(
        runner,
        "_prepare_browser_route",
        lambda **_kwargs: FakeRoute(),
    )
    monkeypatch.setattr(
        runner,
        "_run_browser",
        lambda _api, customer_id, email, **_kwargs: AutomationResult(
            customer_id=customer_id,
            email=email,
        ),
    )

    result = runner.run()

    assert len(create_calls) == 1
    assert create_calls[0]["resume_customer_id"] == 701
    assert result.customer_id == 701
    assert result.email == "pending-701@example.test"
    assert customer_events[0]["customer_id"] == 701


def test_old_api_uses_serial_server_revalidation_without_pending_endpoint(
    monkeypatch,
):
    pending_calls = []
    class FakeAdminApi:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def connect(self):
            return {"ok": True, "api_version": 7}

        def pending_customers(self):
            pending_calls.append(True)
            raise AssertionError("旧 API 不应依赖 pending 列表端点")

    monkeypatch.setattr(automation_module, "AdminApi", FakeAdminApi)
    messages = []
    runner = CTExcelBatchAutomation(
        AppConfig(
            continuous_enabled=True,
            continuous_count=8,
            continuous_workers=5,
        ),
        log=messages.append,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
        item_started=lambda *_args: None,
        item_completed=lambda *_args: None,
    )
    serial_calls = []
    monkeypatch.setattr(
        runner,
        "_run_serial",
        lambda **kwargs: serial_calls.append(kwargs) or "serial-result",
    )
    monkeypatch.setattr(
        runner,
        "_run_parallel",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("旧 API 必须降为单线程")
        ),
    )

    result = runner.run()

    assert result == "serial-result"
    assert serial_calls == [{"total": 8, "completed": 0}]
    assert runner.legacy_api_serial_required is True
    assert runner.resume_customer_ids_by_ordinal == {}
    assert pending_calls == []
    assert any("单线程" in item for item in messages)
    assert any("已暂停配置的 5 线程并发" in item for item in messages)


def test_qg_allocator_retries_duplicate_ip_and_assigns_unique_nodes(
    monkeypatch,
):
    responses = iter(
        [
            {"server": "http://198.51.100.1:10001"},
            {"server": "http://198.51.100.1:10002"},
            {"server": "http://198.51.100.2:10003"},
        ]
    )
    monkeypatch.setattr(
        "ctexcel_client.automation.resolve_proxy",
        lambda _config: next(responses),
    )
    config = AppConfig(
        proxy=ProxyConfig(
            mode="api",
            api_url="https://share.proxy.qg.net/get?num=1",
            api_key="test-key",
        )
    )
    runner = CTExcelBatchAutomation(
        config,
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
        item_started=lambda *_args: None,
        item_completed=lambda *_args: None,
    )

    first = runner._next_unique_qg_proxy()
    second = runner._next_unique_qg_proxy()

    assert first["server"] == "http://198.51.100.1:10001"
    assert second["server"] == "http://198.51.100.2:10003"
    assert runner.qg_proxy_ips == {"198.51.100.1", "198.51.100.2"}


def test_loading_overlay_waits_until_the_page_is_stably_ready(monkeypatch):
    class FakePage:
        def __init__(self):
            self.values = [True, True, False, False, False, False, False]
            self.url = "https://example.test/form"
            self.context = SimpleNamespace(pages=[self])

        def evaluate(self, _script):
            loading = self.values.pop(0) if self.values else False
            return {
                "url": self.url,
                "ready_state": "complete",
                "loading": loading,
                "text_signature": "form",
                "field_signature": "fields",
                "visible_content": 5,
                "field_count": 2,
            }

        def is_closed(self):
            return False

        def wait_for_load_state(self, *_args, **_kwargs):
            raise AssertionError("Loading 遮罩稳定后不应再等 networkidle")

    messages = []
    automation = CTExcelAutomation(
        AppConfig(page_timeout_ms=5000, step_timeout_ms=2000),
        log=messages.append,
        stage=lambda _message: None,
        customer_created=lambda _payload: None,
    )
    clock = {"value": 0.0}
    monkeypatch.setattr(
        automation_module.time,
        "monotonic",
        lambda: clock["value"],
    )
    monkeypatch.setattr(
        automation,
        "_wait_interruptibly",
        lambda seconds: clock.__setitem__(
            "value", clock["value"] + seconds
        ),
    )

    automation._wait_for_page_ready(
        FakePage(),
        "自动续订开关",
        stable_seconds=0.5,
    )

    assert messages == [
        "等待页面加载完成：自动续订开关",
        "页面加载完成：自动续订开关",
    ]


def test_loading_overlay_stalled_for_twenty_seconds_restarts(monkeypatch):
    class FakePage:
        url = "https://example.test/loading"
        context = SimpleNamespace(pages=[])

        def evaluate(self, _script):
            return {
                "url": self.url,
                "ready_state": "interactive",
                "loading": True,
                "text_signature": "unchanged",
                "field_signature": "none",
                "visible_content": 1,
                "field_count": 0,
            }

        def is_closed(self):
            return False

    clock = {"value": 0.0}
    monkeypatch.setattr(
        automation_module.time,
        "monotonic",
        lambda: clock["value"],
    )
    runner = CTExcelAutomation(
        AppConfig(page_timeout_ms=120000),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    monkeypatch.setattr(
        runner,
        "_wait_interruptibly",
        lambda seconds: clock.__setitem__(
            "value", clock["value"] + seconds
        ),
    )

    with pytest.raises(RetryableStalledPageError, match="20 秒"):
        runner._wait_for_page_ready(FakePage(), "首页 Loading")


def test_progress_fingerprint_and_url_match_ignore_query_noise():
    first = {
        "url": "https://example.test/freecard/activityPagefillInfos?a=1",
        "ready_state": "complete",
        "loading": False,
        "text_signature": "10:20",
        "field_signature": "5:6",
        "visible_content": 8,
        "field_count": 4,
        "page_count": 1,
    }
    second = {**first, "irrelevant": "ignored"}

    assert page_progress_fingerprint(first) == page_progress_fingerprint(second)
    assert page_url_matches_path(
        first["url"],
        "/freecard/activityPagefillInfos",
    )
    assert page_url_matches_path(
        "https://www.ctexcel.com/uk/buycard/simcarddetails/1",
        "/buycard/simcarddetails",
    )
    assert not page_url_matches_path(
        "https://www.ctexcel.com/uk/buycard/simcarddetails-old/1",
        "/buycard/simcarddetails",
    )
    assert is_ctexcel_url("https://www.ctexcel.com/uk/tariffQuery/0")
    assert is_ctexcel_url("https://ctexcel.com/freecard/home")
    assert not is_ctexcel_url("http://www.ctexcel.com/uk/tariffQuery/0")
    assert not is_ctexcel_url("https://example.test/uk/tariffQuery/0")


def test_next_click_timeout_continues_when_destination_url_already_loaded(
    monkeypatch,
):
    class FakePage:
        def __init__(self):
            self.url = "https://www.ctexcel.com/freecard/config"
            self.context = SimpleNamespace(pages=[self])

        def get_by_role(self, *_args, **_kwargs):
            return object()

        def evaluate(self, _script):
            return False

        def is_closed(self):
            return False

    page = FakePage()

    class FakeLocator:
        retry_count = 0

        def click(self, **_kwargs):
            page.url = (
                "https://www.ctexcel.com/freecard/activityPagefillInfos"
            )
            raise automation_module.PlaywrightTimeoutError("click timeout")

        def evaluate(self, _script):
            self.retry_count += 1

    locator = FakeLocator()
    messages = []
    runner = CTExcelAutomation(
        AppConfig(),
        log=messages.append,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    monkeypatch.setattr(runner, "_wait_for_page_ready", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "_visible_locator", lambda *_a, **_k: locator)

    runner._click_button_and_wait_for_page(
        page,
        "下一步",
        label="£1 领卡资料页",
        expected_path="/freecard/activityPagefillInfos",
        ready_script="FORM_READY",
    )

    assert locator.retry_count == 0
    assert any("目标网址已出现" in item for item in messages)


def test_transition_accepts_form_marker_when_url_has_not_updated(monkeypatch):
    class FakePage:
        url = "https://example.test/freecard/config"
        context = SimpleNamespace(pages=[])

        def get_by_role(self, *_args, **_kwargs):
            return object()

        def evaluate(self, script):
            return script == "FORM_READY"

        def is_closed(self):
            return False

    class FakeLocator:
        def click(self, **_kwargs):
            return None

        def evaluate(self, _script):
            raise AssertionError("表单已出现时不应重试点击")

    messages = []
    runner = CTExcelAutomation(
        AppConfig(),
        log=messages.append,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    monkeypatch.setattr(runner, "_wait_for_page_ready", lambda *_a, **_k: None)
    monkeypatch.setattr(
        runner,
        "_visible_locator",
        lambda *_a, **_k: FakeLocator(),
    )

    runner._click_button_and_wait_for_page(
        FakePage(),
        "下一步",
        label="£1 领卡资料页",
        expected_path="/freecard/activityPagefillInfos",
        ready_script="FORM_READY",
    )

    assert any("目标表单已出现" in item for item in messages)


def test_select_plan_accepts_detail_dom_without_waiting_for_navigation(
    monkeypatch,
):
    calls = []

    class FakePage:
        url = "https://www.ctexcel.com/uk/tariffQuery/0"

        def evaluate(self, script):
            calls.append(script)
            return True

        def wait_for_url(self, *_args, **_kwargs):
            raise AssertionError(
                "套餐选择应通过页面状态检测，不应阻塞等待 URL"
            )

    runner = CTExcelAutomation(
        AppConfig(),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    monkeypatch.setattr(
        runner,
        "_open_registration_entry",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        runner,
        "_dismiss_cookie_consent",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        runner,
        "_wait_for_page_ready",
        lambda *_args, **_kwargs: None,
    )
    transition = {}

    def fake_transition(_page, **kwargs):
        transition.update(kwargs)

    monkeypatch.setattr(runner, "_wait_for_page_transition", fake_transition)

    runner._select_plan(FakePage())

    assert calls == [automation_module.SELECT_50GB_PLAN_SCRIPT]
    assert transition["expected_path"] == "/buycard/simcarddetails"
    assert transition["ready_script"] == automation_module.PLAN_DETAILS_READY_SCRIPT
    assert transition["stall_timeout_ms"] == (
        automation_module.PLAN_DETAILS_STALL_TIMEOUT_MS
    )
    assert transition.get("retry_action") is None


def test_transition_retries_once_when_first_click_has_no_effect(monkeypatch):
    clock = {"value": 0.0}

    class FakePage:
        url = "https://example.test/freecard/config"
        context = SimpleNamespace(pages=[])
        form_ready = False

        def get_by_role(self, *_args, **_kwargs):
            return object()

        def evaluate(self, script):
            if script == "FORM_READY":
                return self.form_ready
            return {
                "url": self.url,
                "ready_state": "complete",
                "loading": False,
                "text_signature": "same",
                "field_signature": "same",
                "visible_content": 3,
                "field_count": 0,
            }

        def is_closed(self):
            return False

    page = FakePage()

    class FakeLocator:
        retry_count = 0

        def click(self, **_kwargs):
            return None

        def evaluate(self, _script):
            self.retry_count += 1
            page.form_ready = True

    locator = FakeLocator()
    messages = []
    runner = CTExcelAutomation(
        AppConfig(),
        log=messages.append,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    monkeypatch.setattr(runner, "_wait_for_page_ready", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "_visible_locator", lambda *_a, **_k: locator)
    monkeypatch.setattr(
        automation_module.time,
        "monotonic",
        lambda: clock["value"],
    )
    monkeypatch.setattr(
        runner,
        "_wait_interruptibly",
        lambda seconds: clock.__setitem__(
            "value", clock["value"] + seconds
        ),
    )

    runner._click_button_and_wait_for_page(
        page,
        "下一步",
        label="£1 领卡资料页",
        expected_path="/freecard/activityPagefillInfos",
        ready_script="FORM_READY",
    )

    assert locator.retry_count == 1
    assert any("自动重试一次" in item for item in messages)


def test_transition_propagates_retry_automation_errors(monkeypatch):
    clock = {"value": 0.0}

    class FakePage:
        url = "https://example.test/freecard/config"
        context = SimpleNamespace(pages=[])

        def evaluate(self, script):
            if script == "FORM_READY":
                return False
            return {
                "url": self.url,
                "ready_state": "complete",
                "loading": False,
                "text_signature": "same",
                "field_signature": "same",
                "visible_content": 3,
                "field_count": 0,
            }

        def is_closed(self):
            return False

    runner = CTExcelAutomation(
        AppConfig(page_timeout_ms=120000),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    monkeypatch.setattr(
        automation_module.time,
        "monotonic",
        lambda: clock["value"],
    )
    monkeypatch.setattr(
        runner,
        "_wait_interruptibly",
        lambda seconds: clock.__setitem__(
            "value", clock["value"] + seconds
        ),
    )

    def failed_retry():
        raise AutomationError("套餐按钮已离开当前页面")

    with pytest.raises(AutomationError, match="套餐按钮"):
        runner._wait_for_page_transition(
            FakePage(),
            label="套餐详情",
            expected_path="/buycard/simcarddetails",
            ready_script="FORM_READY",
            retry_action=failed_retry,
        )


def test_transition_accepts_slow_detail_dom_with_extended_idle_budget(monkeypatch):
    clock = {"value": 0.0}

    class FakePage:
        url = "https://www.ctexcel.com/uk/tariffQuery/0"
        context = SimpleNamespace(pages=[])

        def evaluate(self, script):
            if script == "PLAN_DETAILS_READY":
                return clock["value"] >= 30.0
            return {
                "url": self.url,
                "ready_state": "interactive",
                "loading": False,
                "text_signature": "unchanged",
                "field_signature": "none",
                "visible_content": 5,
                "field_count": 0,
            }

        def is_closed(self):
            return False

    runner = CTExcelAutomation(
        AppConfig(page_timeout_ms=120000),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    monkeypatch.setattr(
        automation_module.time,
        "monotonic",
        lambda: clock["value"],
    )
    monkeypatch.setattr(
        runner,
        "_wait_interruptibly",
        lambda seconds: clock.__setitem__(
            "value", clock["value"] + seconds
        ),
    )

    runner._wait_for_page_transition(
        FakePage(),
        label="套餐详情",
        expected_path="/buycard/simcarddetails",
        ready_script="PLAN_DETAILS_READY",
        stall_timeout_ms=45_000,
    )

    assert clock["value"] >= 30.0


def test_transition_can_exceed_twenty_seconds_while_dom_keeps_progressing(
    monkeypatch,
):
    clock = {"value": 0.0}

    class FakePage:
        url = "https://example.test/freecard/config"
        context = SimpleNamespace(pages=[])

        def evaluate(self, script):
            if script == "FORM_READY":
                return clock["value"] >= 25.0
            step = int(clock["value"] // 5)
            return {
                "url": self.url,
                "ready_state": "interactive",
                "loading": True,
                "text_signature": f"step-{step}",
                "field_signature": f"fields-{step}",
                "visible_content": step,
                "field_count": step,
            }

        def is_closed(self):
            return False

    runner = CTExcelAutomation(
        AppConfig(page_timeout_ms=120000),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    monkeypatch.setattr(
        automation_module.time,
        "monotonic",
        lambda: clock["value"],
    )
    monkeypatch.setattr(
        runner,
        "_wait_interruptibly",
        lambda seconds: clock.__setitem__(
            "value", clock["value"] + seconds
        ),
    )

    runner._wait_for_page_transition(
        FakePage(),
        label="客户资料页",
        expected_path="/freecard/activityPagefillInfos",
        ready_script="FORM_READY",
    )

    assert clock["value"] >= 25.0


def test_transition_restarts_after_twenty_seconds_without_any_change(
    monkeypatch,
):
    clock = {"value": 0.0}

    class FakePage:
        url = "https://example.test/freecard/config"
        context = SimpleNamespace(pages=[])

        def evaluate(self, script):
            if script == "FORM_READY":
                return False
            return {
                "url": self.url,
                "ready_state": "complete",
                "loading": False,
                "text_signature": "same",
                "field_signature": "same",
                "visible_content": 3,
                "field_count": 0,
            }

        def is_closed(self):
            return False

    runner = CTExcelAutomation(
        AppConfig(page_timeout_ms=120000),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    monkeypatch.setattr(
        automation_module.time,
        "monotonic",
        lambda: clock["value"],
    )
    monkeypatch.setattr(
        runner,
        "_wait_interruptibly",
        lambda seconds: clock.__setitem__(
            "value", clock["value"] + seconds
        ),
    )

    with pytest.raises(RetryableStalledPageError, match="连续 20 秒"):
        runner._wait_for_page_transition(
            FakePage(),
            label="客户资料页",
            expected_path="/freecard/activityPagefillInfos",
            ready_script="FORM_READY",
        )


def test_registration_fields_target_real_inputs_instead_of_placeholder_wrappers():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "automation.py"
    ).read_text(encoding="utf-8")

    assert "page.locator(f'input[placeholder=\"{escaped}\"]')" in source
    assert 'page.get_by_placeholder("请填写姓").fill' not in source
    assert 'page.get_by_placeholder("请填写验证码").fill' not in source


def test_country_is_selected_before_registration_fields_at_short_proxy_speed():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "automation.py"
    ).read_text(encoding="utf-8")

    form_start = source.index("def _fill_customer_info")
    form_end = source.index("def _poll_verification_code", form_start)
    form_source = source[form_start:form_end]
    assert form_source.index("self._select_china(page)") < form_source.index(
        '"请填写姓"'
    )
    assert "BROWSER_SLOW_MO_MAX_MS = 250" in source
    assert "PAGE_READY_STABLE_SECONDS = 0.35" in source
    assert '"slow_mo": min(' in source
    assert 'page.wait_for_load_state(\n                                "networkidle"' not in source
    assert "' el-select '" in source


def test_verification_timestamp_parser_supports_provider_formats():
    milliseconds = parse_message_timestamp("1784918151251")
    iso_time = parse_message_timestamp("2026-07-24T18:35:51.251Z")

    assert milliseconds is not None
    assert milliseconds.timestamp() == 1784918151.251
    assert iso_time == datetime(
        2026,
        7,
        24,
        18,
        35,
        51,
        251000,
        tzinfo=timezone.utc,
    )


def test_verification_freshness_rejects_baseline_and_old_messages():
    requested_at = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

    same_message = assess_verification_freshness(
        {
            "message_id": "old-message",
            "received_at": "2026-07-29T12:00:05Z",
        },
        baseline_message_id="old-message",
        requested_at=requested_at,
    )
    old_timestamp = assess_verification_freshness(
        {
            "message_id": "different-old-message",
            "received_at": "2026-07-29T11:50:00Z",
        },
        baseline_message_id="old-message",
        requested_at=requested_at,
    )

    assert same_message[0] is False
    assert same_message[1] == "邮件 ID 与发送前相同"
    assert old_timestamp[0] is False
    assert old_timestamp[1] == "邮件收件时间早于本次请求"


def test_verification_freshness_accepts_new_message_after_request():
    requested_at = datetime.now(timezone.utc)
    fresh = assess_verification_freshness(
        {
            "message_id": "new-message",
            "received_at": (
                requested_at + timedelta(seconds=4)
            ).isoformat(),
        },
        baseline_message_id="old-message",
        requested_at=requested_at,
    )

    assert fresh[0] is True
    assert fresh[1] == "验证码邮件属于本次请求"


def test_verification_cooldown_notice_is_recognized():
    assert verification_cooldown_message(
        "提示：180秒之内不要重复操作哦~"
    ) == "180秒之内不要重复操作"
    assert verification_cooldown_message("验证码发送成功") == ""


def test_verification_cooldown_waits_for_delayed_proxy_feedback(monkeypatch):
    clock = {"value": 0.0}
    evaluations = {"count": 0}
    runner = CTExcelAutomation(
        AppConfig(),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )

    class FakePage:
        def evaluate(self, _script):
            evaluations["count"] += 1
            if clock["value"] >= 2.0:
                return "180秒之内不要重复操作哦~"
            return ""

    monkeypatch.setattr(
        automation_module.time,
        "monotonic",
        lambda: clock["value"],
    )
    monkeypatch.setattr(
        runner,
        "_wait_interruptibly",
        lambda seconds: clock.__setitem__(
            "value", clock["value"] + seconds
        ),
    )

    notice = runner._visible_verification_cooldown(FakePage())

    assert notice == "180秒之内不要重复操作"
    assert clock["value"] >= 2.0
    assert evaluations["count"] > 1


def test_verification_feedback_success_does_not_add_five_second_delay(
    monkeypatch,
):
    runner = CTExcelAutomation(
        AppConfig(),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )

    class FakePage:
        calls = 0

        def evaluate(self, _script):
            self.calls += 1
            return "验证码发送成功" if self.calls == 1 else ""

    waits = []
    monkeypatch.setattr(runner, "_wait_interruptibly", waits.append)

    assert runner._visible_verification_cooldown(FakePage()) == ""
    assert waits == [0.1]


def test_stale_success_feedback_does_not_mask_delayed_cooldown(monkeypatch):
    runner = CTExcelAutomation(
        AppConfig(),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    feedback = iter(
        [
            "验证码发送成功",
            "验证码发送成功",
            "验证码发送成功 | 180秒之内不要重复操作哦~",
        ]
    )

    class FakePage:
        def evaluate(self, _script):
            return next(feedback)

    monkeypatch.setattr(runner, "_wait_interruptibly", lambda _seconds: None)

    assert runner._visible_verification_cooldown(FakePage()) == (
        "180秒之内不要重复操作"
    )


def test_browser_retry_reuses_cached_verification_without_resending(
    monkeypatch,
):
    messages = []
    runner = CTExcelAutomation(
        AppConfig(),
        log=messages.append,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    runner.cached_verification_customer_id = 701
    runner.cached_verification_code = "123456"
    runner.cached_verification_at = 100.0
    monkeypatch.setattr(
        automation_module.time,
        "monotonic",
        lambda: 120.0,
    )
    monkeypatch.setattr(
        runner,
        "_click_visible_text",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("缓存验证码有效时不应再次点击获取验证码")
        ),
    )

    class FakeApi:
        def verification_code(self, customer_id):
            assert customer_id == 701
            return {
                "found": True,
                "code": "123456",
                "message_id": "cached-message",
                "received_at": datetime.now(timezone.utc).isoformat(),
            }

    code = runner._obtain_verification_code(
        object(),
        FakeApi(),
        701,
    )

    assert code == "123456"
    assert any("跳过 180 秒内的重复发送" in item for item in messages)


def test_cooldown_reuses_existing_mail_instead_of_waiting_for_new_one(
    monkeypatch,
):
    calls = []

    class FakeApi:
        def verification_code(self, customer_id):
            assert customer_id == 702
            calls.append(customer_id)
            if len(calls) == 1:
                return {
                    "found": True,
                    "code": "111111",
                    "message_id": "stale-message",
                    "received_at": (
                        datetime.now(timezone.utc) - timedelta(minutes=10)
                    ).isoformat(),
                }
            return {
                "found": True,
                "code": "654321",
                "message_id": "new-message",
                "received_at": datetime.now(timezone.utc).isoformat(),
            }

    messages = []
    clicks = []
    runner = CTExcelAutomation(
        AppConfig(),
        log=messages.append,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    monkeypatch.setattr(
        runner,
        "_click_visible_text",
        lambda _page, text: clicks.append(text),
    )
    monkeypatch.setattr(
        runner,
        "_visible_verification_cooldown",
        lambda _page: "180秒之内不要重复操作",
    )
    monkeypatch.setattr(
        runner,
        "_poll_verification_code",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("冷却期不应等待一封不会出现的新邮件")
        ),
    )

    code = runner._obtain_verification_code(
        object(),
        FakeApi(),
        702,
    )

    assert code == "654321"
    assert len(calls) == 2
    assert clicks == ["获取验证码"]
    assert runner.cached_verification_code == "654321"
    assert any("冷却期近期验证码已复核并复用" in item for item in messages)


def test_recent_verification_code_rejects_missing_or_old_timestamp():
    now = datetime.now(timezone.utc)
    assert recent_verification_code(
        {
            "found": True,
            "code": "123456",
            "received_at": now.isoformat(),
        },
        now=now,
    ) == "123456"
    assert recent_verification_code(
        {
            "found": True,
            "code": "123456",
            "received_at": (now - timedelta(minutes=4)).isoformat(),
        },
        now=now,
    ) == ""
    assert recent_verification_code(
        {"found": True, "code": "123456"},
        now=now,
    ) == ""


def test_coupon_rejection_is_reported_instead_of_looking_like_missing_input():
    assert coupon_rejection_message(
        "提示：优惠券不存在或已过期"
    ) == "优惠券不存在或已过期"
    assert coupon_rejection_message("订单金额：£5.95") == ""


def test_pending_customer_confirmation_email_prevents_reuse():
    messages = []

    class FakeApi:
        def pending_customers(self):
            return [
                {
                    "customer_id": 488,
                    "email": "confirmed@example.test",
                    "phone_number": None,
                    "order_number": None,
                    "registration_confirmed_at": None,
                }
            ]

        def sync_order_info(self, customer_id):
            assert customer_id == 488
            return {
                "found": True,
                "registration_confirmed": True,
                "registration_confirmed_at": "2026-07-31T07:05:00Z",
                "phone_number": None,
                "order_number": None,
            }

    automation = CTExcelAutomation(
        AppConfig(),
        log=messages.append,
        stage=lambda _message: None,
        customer_created=lambda _payload: None,
    )
    automation._refresh_pending_customers(FakeApi())

    assert any("标记为注册成功并跳过复用" in item for item in messages)
    assert any("后续申请将使用新客户邮箱" in item for item in messages)
