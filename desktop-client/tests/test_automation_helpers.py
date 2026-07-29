from decimal import Decimal
from pathlib import Path

from ctexcel_client.automation import (
    normalize_money,
    parse_success_text,
    price_is_expected,
)


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
    assert "错误现场已保留" in source
    assert "error_browser_hold_seconds" in source
