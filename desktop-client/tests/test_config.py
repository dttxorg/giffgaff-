from pathlib import Path
import os

from ctexcel_client import __version__
from ctexcel_client.config import (
    AppConfig,
    DEFAULT_PROXY_API_URL,
    ProxyConfig,
    PURCHASE_ROUTE_50GB,
    PURCHASE_ROUTE_FREECARD,
    RegistrationDefaults,
    TelegramConfig,
    display_qg_proxy_api_url,
    is_qg_proxy_api_url,
    load_config,
    save_config,
    split_qg_proxy_api_key,
)


LEGACY_CLIPROXY_API_URL = (
    "https://api.cliproxy.io/white/api"
    "?region=Rand&num=1&time=10&format=n&type=txt"
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
    assert '"并发线程"' in source
    assert "self.continuous_workers.setRange(1, 10)" in source
    assert '"Telegram 付款提醒"' in source
    assert '"测试推送"' in source
    assert __version__ == "2.4.1"
    assert 'f"CTExcel 申请工作台 v{__version__}"' in source


def test_credentials_are_not_written_as_plaintext(tmp_path: Path):
    target = tmp_path / "config.json"
    config = AppConfig(
        app_password="super-secret-password",
        remember_credentials=True,
        proxy=ProxyConfig(
            password="super-secret-proxy-password",
            api_key="super-secret-qg-key",
            pool=(
                "proxy.example.test:3010:"
                "super-secret-pool-user:super-secret-pool-password"
            ),
        ),
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
    assert "super-secret-qg-key" not in raw
    assert "super-secret-pool-user" not in raw
    assert "super-secret-pool-password" not in raw
    assert "fixed shipping address" in raw
    if os.name == "nt":
        loaded = load_config(target)
        assert loaded.app_password == "super-secret-password"
        assert loaded.proxy.password == "super-secret-proxy-password"
        assert loaded.proxy.api_key == "super-secret-qg-key"
        assert "super-secret-pool-user" in loaded.proxy.pool


def test_qg_key_embedded_in_url_is_removed_before_config_write(tmp_path: Path):
    target = tmp_path / "config.json"
    secret = "embedded-secret-qg-key"
    config = AppConfig(
        remember_credentials=True,
        proxy=ProxyConfig(
            mode="api",
            api_url=(
                "https://share.proxy.qg.net/get"
                f"?key={secret}&area=350500&num=1&distinct=true"
            ),
        ),
    )

    save_config(config, target)

    raw = target.read_text(encoding="utf-8")
    assert secret not in raw
    assert "key=" not in raw
    assert "area=350500" in raw


def test_qg_full_extraction_link_round_trip_preserves_parameters():
    full_url = (
        "https://share.proxy.qg.net/get?key=sample-key&num=1&area="
        "&isp=0&format=txt&seq=\\r\\n&distinct=false"
    )

    sanitized, api_key = split_qg_proxy_api_key(full_url)
    displayed = display_qg_proxy_api_url(sanitized, api_key)

    assert api_key == "sample-key"
    assert sanitized == (
        "https://share.proxy.qg.net/get?num=1&area=&isp=0&format=txt"
        "&seq=\\r\\n&distinct=false"
    )
    assert displayed == full_url


def test_proxy_ui_exposes_fixed_and_dynamic_socks5_modes():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "main_window.py"
    ).read_text(encoding="utf-8")

    assert '"粘贴单条代理", "custom"' in source
    assert '"批量代理池", "pool"' in source
    assert '"API 动态提取", "api"' in source
    assert '"SOCKS5", "socks5"' in source
    assert "提取并测试" in source
    assert "hostname:port:username:password" in source
    assert "从剪贴板导入" in source
    assert "粘贴代理池" in source
    assert "self.proxy_pool_uses_min.setValue(5)" in source
    assert "self.proxy_pool_uses_max.setValue(8)" in source
    assert "当前出口公网 IP" in source
    assert "background-color: #ffffff" in source
    assert "Qt.TextSelectableByMouse" in source
    assert 'self.proxy_api_url_label = self._field_label("完整提取链接")' in source
    assert "self.proxy_api_key" not in source
    assert "直接粘贴服务商生成的完整 /get 链接" in source
    assert is_qg_proxy_api_url(DEFAULT_PROXY_API_URL)


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
        % LEGACY_CLIPROXY_API_URL,
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
        continuous_workers=6,
        telegram=TelegramConfig(
            enabled=True,
            bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd",
            chat_id="-1001234567890",
        ),
        registration=RegistrationDefaults(
            last_name="Fixed",
            first_name="Name",
            contact_phone="13800000000",
            contact_phone_end="13800000999",
            chinese_address="fixed shipping address",
            address_suffix_start=1,
            address_suffix_end=1000,
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
    assert loaded.continuous_workers == 6
    assert (
        "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd"
        not in target.read_text(encoding="utf-8")
    )
    assert loaded.telegram.enabled is True
    assert loaded.telegram.chat_id == "-1001234567890"
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
          "continuous_workers": 50,
          "continuous_interval_seconds": -3
        }
        """,
        encoding="utf-8",
    )

    loaded = load_config(target)

    assert loaded.continuous_enabled is True
    assert loaded.continuous_count == 1000
    assert loaded.continuous_workers == 10
    assert loaded.continuous_interval_seconds == 0
