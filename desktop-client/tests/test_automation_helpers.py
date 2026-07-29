from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ctexcel_client.automation import (
    CTExcelAutomation,
    assess_verification_freshness,
    coupon_rejection_message,
    normalize_money,
    parse_message_timestamp,
    parse_success_text,
    price_is_expected,
)
from ctexcel_client.config import AppConfig


def test_money_and_discount_price_parsing():
    assert normalize_money("£ 5.95") == Decimal("5.95")
    assert normalize_money("not-a-price") is None
    assert price_is_expected("订单金额：£5.95", "5.95") is True
    assert price_is_expected("订单金额：£11.90", "5.95") is False


def test_success_page_fields_are_read_for_operator_summary_only():
    parsed = parse_success_text(
        """
        订购成功
        订单号：ORDER2026072912345678901
        手机号码：07900000009
        交易金额：£ 5.95
        """
    )

    assert parsed == {
        "order_number": "ORDER2026072912345678901",
        "phone_number": "07900000009",
        "transaction_amount": "5.95",
    }


def test_sim_configuration_tracks_current_page_dom_and_preserves_errors():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "automation.py"
    ).read_text(encoding="utf-8")

    assert '"实体SIM卡",\n            exact=False' in source
    assert "button.uc-deny-button" in source
    assert 'page.locator(".el-switch")' in source
    assert "'.el-loading-mask'" in source
    assert "Loading 遮罩持续未消失" in source
    assert '"networkidle"' in source
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


def test_loading_overlay_waits_until_the_page_is_stably_ready():
    class FakePage:
        def __init__(self):
            self.values = [True, True, False, False, False, False]

        def evaluate(self, _script):
            return self.values.pop(0) if self.values else False

        def is_closed(self):
            return False

        def wait_for_load_state(self, state, timeout):
            assert state == "networkidle"
            assert timeout == 3000

    messages = []
    automation = CTExcelAutomation(
        AppConfig(page_timeout_ms=5000, step_timeout_ms=2000),
        log=messages.append,
        stage=lambda _message: None,
        customer_created=lambda _payload: None,
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


def test_registration_fields_target_real_inputs_instead_of_placeholder_wrappers():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "automation.py"
    ).read_text(encoding="utf-8")

    assert "page.locator(f'input[placeholder=\"{escaped}\"]')" in source
    assert 'page.get_by_placeholder("请填写姓").fill' not in source
    assert 'page.get_by_placeholder("请填写验证码").fill' not in source


def test_country_is_selected_before_registration_fields_at_human_paced_speed():
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
    assert '"slow_mo": max(800, int(self.config.slow_mo_ms))' in source
    assert "stable_seconds: float = 1.2" in source
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


def test_coupon_rejection_is_reported_instead_of_looking_like_missing_input():
    assert coupon_rejection_message(
        "提示：优惠券不存在或已过期"
    ) == "优惠券不存在或已过期"
    assert coupon_rejection_message("订单金额：£5.95") == ""
