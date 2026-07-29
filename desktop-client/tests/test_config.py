from pathlib import Path
import os

from ctexcel_client.config import (
    AppConfig,
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


def test_credentials_are_not_written_as_plaintext(tmp_path: Path):
    target = tmp_path / "config.json"
    config = AppConfig(
        app_password="super-secret-password",
        remember_credentials=True,
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
    assert "fixed shipping address" in raw
    if os.name == "nt":
        loaded = load_config(target)
        assert loaded.app_password == "super-secret-password"


def test_non_secret_registration_defaults_round_trip(tmp_path: Path):
    target = tmp_path / "config.json"
    config = AppConfig(
        remember_credentials=False,
        registration=RegistrationDefaults(
            last_name="Fixed",
            first_name="Name",
            contact_phone="13800000000",
            chinese_address="fixed shipping address",
            referral_code="REFCODE",
            coupon_code="HALF",
            expected_price_gbp="5.95",
        ),
    )

    save_config(config, target)
    loaded = load_config(target)

    assert loaded.registration == config.registration
    assert loaded.app_password == ""
