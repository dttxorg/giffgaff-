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
    assert '"slow_mo": max(350, int(self.config.slow_mo_ms))' in source
    assert "' el-select '" in source
