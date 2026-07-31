from __future__ import annotations

from dataclasses import asdict, dataclass, field
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus, unquote_plus, urlsplit, urlunsplit


APP_NAME = "CTExcelApplyClient"
CRYPTPROTECT_UI_FORBIDDEN = 0x1
PURCHASE_ROUTE_50GB = "plan_50gb"
PURCHASE_ROUTE_FREECARD = "freecard_1gbp"
PURCHASE_ROUTES = {
    PURCHASE_ROUTE_50GB,
    PURCHASE_ROUTE_FREECARD,
}
DEFAULT_APPLICATION_URL = (
    "https://www.ctexcel.com/uk/buyCard/buyCardPackage/1"
    "?recommendCode=NTKWJX"
)
FREECARD_APPLICATION_URL = "https://www.ctexcel.com/freecard/home"
DEFAULT_PROXY_API_URL = (
    "https://share.proxy.qg.net/get?num=1&distinct=true"
)
DEFAULT_SLOW_MO_MS = 100
LEGACY_SLOW_MO_MS = 800
DEFAULT_VERIFICATION_MIN_WAIT_SECONDS = 3
LEGACY_VERIFICATION_MIN_WAIT_SECONDS = 8


def is_qg_proxy_api_url(value: str) -> bool:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and (parsed.hostname or "").lower() == "share.proxy.qg.net"
        and parsed.path.rstrip("/").lower() == "/get"
    )


def split_qg_proxy_api_key(value: str) -> tuple[str, str]:
    """Remove an embedded QG key so it can be stored as a protected secret."""
    raw = str(value or "").strip()
    if not is_qg_proxy_api_url(raw):
        return raw, ""
    parsed = urlsplit(raw)
    api_key = ""
    filtered: list[str] = []
    for item in parsed.query.split("&"):
        name, separator, item_value = item.partition("=")
        if unquote_plus(name) == "key":
            api_key = unquote_plus(item_value).strip() if separator else ""
        else:
            filtered.append(item)
    sanitized = urlunsplit(parsed._replace(query="&".join(filtered)))
    return sanitized, api_key


def display_qg_proxy_api_url(value: str, api_key: str) -> str:
    """Rebuild the full QG extraction link shown in the single URL field."""
    raw, embedded_api_key = split_qg_proxy_api_key(value)
    key = embedded_api_key or str(api_key or "").strip()
    if not key or not is_qg_proxy_api_url(raw):
        return raw
    parsed = urlsplit(raw)
    key_item = f"key={quote_plus(key)}"
    query = f"{key_item}&{parsed.query}" if parsed.query else key_item
    return urlunsplit(parsed._replace(query=query))


def is_cliproxy_whitelist_url(value: str) -> bool:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and (parsed.hostname or "").lower() == "api.cliproxy.io"
        and parsed.path.rstrip("/").lower() == "/white/api"
    )


def app_config_dir() -> Path:
    root = os.getenv("APPDATA")
    base = Path(root) if root else Path.home() / ".config"
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return app_config_dir() / "config.json"


def default_user_data_dir() -> str:
    return str(app_config_dir() / "browser-profile")


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _blob_from_bytes(value: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(value)
    blob = _DataBlob(
        len(value),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    return blob, buffer


def protect_secret(value: str) -> str:
    """使用 Windows DPAPI 保存客户端连接口令。

    非 Windows 开发环境不持久化秘密值，避免明文写入配置文件。
    """
    if not value or os.name != "nt":
        return ""
    raw = value.encode("utf-8")
    input_blob, input_buffer = _blob_from_bytes(raw)
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        ctypes.c_wchar_p("CTExcelApplyClient"),
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    _ = input_buffer
    if not ok:
        return ""
    try:
        protected = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return base64.b64encode(protected).decode("ascii")
    finally:
        kernel32.LocalFree(output_blob.pbData)


def unprotect_secret(value: str) -> str:
    if not value or os.name != "nt":
        return ""
    try:
        protected = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, TypeError):
        return ""
    input_blob, input_buffer = _blob_from_bytes(protected)
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    _ = input_buffer
    if not ok:
        return ""
    try:
        raw = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    finally:
        kernel32.LocalFree(output_blob.pbData)


@dataclass
class ProxyConfig:
    mode: str = "none"
    proxy_type: str = "socks5"
    pool: str = ""
    pool_uses_min: int = 5
    pool_uses_max: int = 8
    host: str = ""
    port: str = ""
    username: str = ""
    password: str = ""
    api_url: str = DEFAULT_PROXY_API_URL
    api_key: str = ""
    api_timeout_seconds: int = 20

    def effective_proxy_type(self) -> str:
        if self.mode == "api" and is_cliproxy_whitelist_url(self.api_url):
            return "socks5"
        if self.mode == "api" and is_qg_proxy_api_url(self.api_url):
            return "http"
        if self.mode == "tunnel":
            return "http"
        return self.proxy_type.strip().lower() or "socks5"

    def playwright_proxy(self) -> Optional[dict[str, str]]:
        if self.mode not in {"custom", "tunnel"}:
            return None
        host = self.host.strip()
        port = self.port.strip()
        if not host or not port:
            return None
        proxy_type = self.effective_proxy_type()
        if proxy_type not in {"http", "https", "socks5"}:
            return None
        if not port.isdigit() or not (1 <= int(port) <= 65535):
            return None
        result = {"server": f"{proxy_type}://{host}:{port}"}
        if self.username:
            result["username"] = self.username
        if self.password:
            result["password"] = self.password
        return result


@dataclass
class RegistrationDefaults:
    last_name: str = ""
    first_name: str = ""
    contact_phone: str = ""
    contact_phone_end: str = ""
    chinese_address: str = ""
    address_suffix_start: int = 1
    address_suffix_end: int = 1000
    referral_code: str = "NTKWJX"
    freecard_referrer: str = "447942946765"
    coupon_code: str = "DEAL50OFF"
    expected_price_gbp: str = "5.95"


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class AppConfig:
    server_url: str = "https://gg.6667766.xyz"
    app_password: str = ""
    remember_credentials: bool = True
    purchase_route: str = PURCHASE_ROUTE_FREECARD
    continuous_enabled: bool = False
    continuous_count: int = 100
    continuous_workers: int = 1
    continuous_interval_seconds: int = 3
    application_url: str = DEFAULT_APPLICATION_URL
    browser_channel: str = "msedge"
    user_data_dir: str = field(default_factory=default_user_data_dir)
    headless: bool = False
    slow_mo_ms: int = DEFAULT_SLOW_MO_MS
    page_timeout_ms: int = 120000
    step_timeout_ms: int = 20000
    verification_min_wait_seconds: int = DEFAULT_VERIFICATION_MIN_WAIT_SECONDS
    verification_timeout_seconds: int = 180
    payment_timeout_seconds: int = 1800
    order_sync_timeout_seconds: int = 180
    error_browser_hold_seconds: int = 180
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    registration: RegistrationDefaults = field(default_factory=RegistrationDefaults)


def _merge_config(raw: dict[str, Any]) -> AppConfig:
    proxy_raw = raw.get("proxy") if isinstance(raw.get("proxy"), dict) else {}
    proxy_values = {
        key: value
        for key, value in proxy_raw.items()
        if key in ProxyConfig.__dataclass_fields__
        and key not in {"password", "pool", "api_key"}
    }
    proxy_values["password"] = unprotect_secret(
        str(proxy_raw.get("password_protected") or "")
    )
    # 兼容曾经写入明文 password 的内部开发配置，下一次保存会自动迁移。
    if not proxy_values["password"]:
        proxy_values["password"] = str(proxy_raw.get("password") or "")
    proxy_values["pool"] = unprotect_secret(
        str(proxy_raw.get("pool_protected") or "")
    )
    if not proxy_values["pool"]:
        proxy_values["pool"] = str(proxy_raw.get("pool") or "")
    proxy_values["api_key"] = unprotect_secret(
        str(proxy_raw.get("api_key_protected") or "")
    )
    # 兼容内部开发配置；下一次保存时迁移为 DPAPI 密文。
    if not proxy_values["api_key"]:
        proxy_values["api_key"] = str(proxy_raw.get("api_key") or "")
    for key, default in (("pool_uses_min", 5), ("pool_uses_max", 8)):
        try:
            proxy_values[key] = min(
                100,
                max(1, int(proxy_values.get(key, default))),
            )
        except (TypeError, ValueError):
            proxy_values[key] = default
    if proxy_values["pool_uses_min"] > proxy_values["pool_uses_max"]:
        proxy_values["pool_uses_min"], proxy_values["pool_uses_max"] = (
            proxy_values["pool_uses_max"],
            proxy_values["pool_uses_min"],
        )
    proxy = ProxyConfig(**proxy_values)
    proxy.api_url, embedded_api_key = split_qg_proxy_api_key(proxy.api_url)
    if not proxy.api_key:
        proxy.api_key = embedded_api_key
    # 2.0.6 以前保存过 HTTP 时，Cliproxy 白名单接口会被错误地按 HTTP 使用。
    if proxy.mode == "api" and is_cliproxy_whitelist_url(proxy.api_url):
        proxy.proxy_type = "socks5"
    registration_raw = (
        raw.get("registration")
        if isinstance(raw.get("registration"), dict)
        else {}
    )
    registration = RegistrationDefaults(
        **{
            key: value
            for key, value in registration_raw.items()
            if key in RegistrationDefaults.__dataclass_fields__
        }
    )
    for key, default in (
        ("address_suffix_start", 1),
        ("address_suffix_end", 1000),
    ):
        try:
            setattr(
                registration,
                key,
                min(1_000_000, max(1, int(getattr(registration, key)))),
            )
        except (TypeError, ValueError):
            setattr(registration, key, default)
    if registration.address_suffix_start > registration.address_suffix_end:
        registration.address_suffix_start, registration.address_suffix_end = (
            registration.address_suffix_end,
            registration.address_suffix_start,
        )
    telegram_raw = (
        raw.get("telegram")
        if isinstance(raw.get("telegram"), dict)
        else {}
    )
    telegram_token = unprotect_secret(
        str(telegram_raw.get("bot_token_protected") or "")
    )
    if not telegram_token:
        telegram_token = str(telegram_raw.get("bot_token") or "")
    telegram = TelegramConfig(
        enabled=bool(telegram_raw.get("enabled", False)),
        bot_token=telegram_token,
        chat_id=str(telegram_raw.get("chat_id") or ""),
    )
    values = {
        key: value
        for key, value in raw.items()
        if key in AppConfig.__dataclass_fields__
        and key not in {
            "proxy",
            "telegram",
            "registration",
            "app_password",
        }
    }
    if values.get("purchase_route") not in PURCHASE_ROUTES:
        values["purchase_route"] = PURCHASE_ROUTE_FREECARD
    try:
        values["continuous_count"] = min(
            1000,
            max(1, int(values.get("continuous_count", 100))),
        )
    except (TypeError, ValueError):
        values["continuous_count"] = 100
    try:
        values["continuous_workers"] = min(
            10,
            max(1, int(values.get("continuous_workers", 1))),
        )
    except (TypeError, ValueError):
        values["continuous_workers"] = 1
    try:
        values["continuous_interval_seconds"] = min(
            60,
            max(0, int(values.get("continuous_interval_seconds", 3))),
        )
    except (TypeError, ValueError):
        values["continuous_interval_seconds"] = 3
    try:
        slow_mo_ms = int(
            values.get("slow_mo_ms", DEFAULT_SLOW_MO_MS)
        )
    except (TypeError, ValueError):
        slow_mo_ms = DEFAULT_SLOW_MO_MS
    # v2.5.4 及更早版本把 800ms 当作内部默认值持久化。
    # 这里自动迁移，否则升级后仍会每次 Playwright 操作强制等待 800ms。
    if slow_mo_ms == LEGACY_SLOW_MO_MS:
        slow_mo_ms = DEFAULT_SLOW_MO_MS
    values["slow_mo_ms"] = min(250, max(0, slow_mo_ms))
    try:
        verification_wait = int(
            values.get(
                "verification_min_wait_seconds",
                DEFAULT_VERIFICATION_MIN_WAIT_SECONDS,
            )
        )
    except (TypeError, ValueError):
        verification_wait = DEFAULT_VERIFICATION_MIN_WAIT_SECONDS
    if verification_wait == LEGACY_VERIFICATION_MIN_WAIT_SECONDS:
        verification_wait = DEFAULT_VERIFICATION_MIN_WAIT_SECONDS
    values["verification_min_wait_seconds"] = min(
        30,
        max(3, verification_wait),
    )
    values["app_password"] = unprotect_secret(
        str(raw.get("app_password_protected") or "")
    )
    return AppConfig(
        **values,
        proxy=proxy,
        telegram=telegram,
        registration=registration,
    )


def load_config(path: Optional[Path] = None) -> AppConfig:
    target = path or config_path()
    if not target.exists():
        return AppConfig()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return _merge_config(raw)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return AppConfig()


def save_config(config: AppConfig, path: Optional[Path] = None) -> None:
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = asdict(config)
    raw.pop("app_password", None)
    proxy_raw = raw.get("proxy") if isinstance(raw.get("proxy"), dict) else {}
    proxy_raw.pop("password", None)
    proxy_raw.pop("pool", None)
    proxy_raw.pop("api_key", None)
    sanitized_api_url, embedded_api_key = split_qg_proxy_api_key(
        config.proxy.api_url
    )
    proxy_raw["api_url"] = sanitized_api_url
    api_key = config.proxy.api_key or embedded_api_key
    telegram_raw = (
        raw.get("telegram")
        if isinstance(raw.get("telegram"), dict)
        else {}
    )
    telegram_raw.pop("bot_token", None)
    if config.remember_credentials:
        raw["app_password_protected"] = protect_secret(config.app_password)
        proxy_raw["password_protected"] = protect_secret(config.proxy.password)
        proxy_raw["pool_protected"] = protect_secret(config.proxy.pool)
        proxy_raw["api_key_protected"] = protect_secret(api_key)
        telegram_raw["bot_token_protected"] = protect_secret(
            config.telegram.bot_token
        )
    else:
        raw["app_password_protected"] = ""
        proxy_raw["password_protected"] = ""
        proxy_raw["pool_protected"] = ""
        proxy_raw["api_key_protected"] = ""
        telegram_raw["bot_token_protected"] = ""
    target.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
