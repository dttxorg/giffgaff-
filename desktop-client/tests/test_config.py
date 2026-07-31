from pathlib import Path
import os

from ctexcel_client.config import (
    AppConfig,
    DEFAULT_PROXY_API_URL,
    ProxyConfig,
    PURCHASE_ROUTE_50GB,
    PURCHASE_ROUTE_FREECARD,
    RegistrationDefaults,
    load_config,
    save_config,
)


def test_client_ui_uses_scoped_api_without_hidden_entry_field():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "main_window.py"
    ).read_text(encoding="utf-8")

    assert "独立的 CTExcel 限权 API" in source
    assert "隐藏管理入口" in source
    assert "entry_path" not in source
    assert "自动申请工作台" in source
    assert '"预存 £1 领卡"' in source
    assert '"50GB · £11.9/30天（优惠后 £5.95）"' in source
    assert "£1 路线推荐人号码" in source
    assert '"连续申请"' in source
    assert '"目标数量"' in source


def test_credentials_are_not_written_as_plaintext(tmp_path: Path):
    target = tmp_path / "config.json"
    config = AppConfig(
        app_password="super-secret-password",
        remember_credentials=True,
        proxy=ProxyConfig(password="super-secret-proxy-password"),
        registration=RegistrationDefaults(
            last_name="Fixed",
            first_name="Name",
            contact_phone="13800000000",
            chinese_address="fixed shipping address",
        ),
    )

    save_config(config, target)

    raw = target.read_text(encoding="utf-8")
    assert "super-secret-password" not in raw
    assert "super-secret-proxy-password" not in raw
    assert "fixed shipping address" in raw
    if os.name == "nt":
        loaded = load_config(target)
        assert loaded.app_password == "super-secret-password"
        assert loaded.proxy.password == "super-secret-proxy-password"


def test_proxy_ui_exposes_fixed_and_dynamic_socks5_modes():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "main_window.py"
    ).read_text(encoding="utf-8")

    assert '"粘贴单条代理", "custom"' in source
    assert '"API 动态提取", "api"' in source
    assert '"SOCKS5", "socks5"' in source
    assert "提取并测试" in source
    assert "hostname:port:username:password" in source
    assert "从剪贴板导入" in source
    assert "当前出口公网 IP" in source
    assert "background-color: #ffffff" in source
    assert "Qt.TextSelectableByMouse" in source


def test_old_cliproxy_http_setting_migrates_to_socks5(tmp_path: Path):
    target = tmp_path / "config.json"
    target.write_text(
        """
        {
          "proxy": {
            "mode": "api",
            "proxy_type": "http",
            "api_url": "%s"
          }
        }
        """
        % DEFAULT_PROXY_API_URL,
        encoding="utf-8",
    )

    loaded = load_config(target)

    assert loaded.proxy.proxy_type == "socks5"
    assert loaded.proxy.effective_proxy_type() == "socks5"


def test_non_secret_registration_defaults_round_trip(tmp_path: Path):
    target = tmp_path / "config.json"
    config = AppConfig(
        remember_credentials=False,
        purchase_route=PURCHASE_ROUTE_50GB,
        continuous_enabled=True,
        continuous_count=100,
        registration=RegistrationDefaults(
            last_name="Fixed",
            first_name="Name",
            contact_phone="13800000000",
            chinese_address="fixed shipping address",
            referral_code="REFCODE",
            freecard_referrer="447942946765",
            coupon_code="HALF",
            expected_price_gbp="5.95",
        ),
    )

    save_config(config, target)
    loaded = load_config(target)

    assert loaded.registration == config.registration
    assert loaded.purchase_route == PURCHASE_ROUTE_50GB
    assert loaded.continuous_enabled is True
    assert loaded.continuous_count == 100
    assert loaded.app_password == ""


def test_invalid_saved_purchase_route_migrates_to_freecard(tmp_path: Path):
    target = tmp_path / "config.json"
    target.write_text(
        '{"purchase_route": "removed-route"}',
        encoding="utf-8",
    )

    loaded = load_config(target)

    assert loaded.purchase_route == PURCHASE_ROUTE_FREECARD


def test_invalid_continuous_values_are_bounded(tmp_path: Path):
    target = tmp_path / "config.json"
    target.write_text(
        """
        {
          "continuous_enabled": true,
          "continuous_count": 50000,
          "continuous_interval_seconds": -3
        }
        """,
        encoding="utf-8",
    )

    loaded = load_config(target)

    assert loaded.continuous_enabled is True
    assert loaded.continuous_count == 1000
    assert loaded.continuous_interval_seconds == 0
