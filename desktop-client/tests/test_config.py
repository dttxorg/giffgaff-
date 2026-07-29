from pathlib import Path
import os

from ctexcel_client.config import (
    AppConfig,
    RegistrationDefaults,
    load_config,
    save_config,
)


def test_credentials_are_not_written_as_plaintext(tmp_path: Path):
    target = tmp_path / "config.json"
    config = AppConfig(
        admin_entry_path="/" + ("x" * 40),
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
    assert "/" + ("x" * 40) not in raw
    assert "fixed shipping address" in raw
    if os.name == "nt":
        loaded = load_config(target)
        assert loaded.app_password == "super-secret-password"
        assert loaded.admin_entry_path == "/" + ("x" * 40)


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
    assert loaded.admin_entry_path == ""
    assert loaded.app_password == ""
