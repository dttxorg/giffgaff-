from __future__ import annotations

from dataclasses import asdict, dataclass, field
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit


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
    "https://api.cliproxy.io/white/api"
    "?region=Rand&num=1&time=10&format=n&type=txt"
)


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
    host: str = ""
    port: str = ""
    username: str = ""
    password: str = ""
    api_url: str = DEFAULT_PROXY_API_URL
    api_timeout_seconds: int = 20

    def effective_proxy_type(self) -> str:
        if self.mode == "api" and is_cliproxy_whitelist_url(self.api_url):
            return "socks5"
        return self.proxy_type.strip().lower() or "socks5"

    def playwright_proxy(self) -> Optional[dict[str, str]]:
        if self.mode != "custom":
            return None
        host = self.host.strip()
        port = self.port.strip()
        if not host or not port:
            return None
        proxy_type = self.proxy_type.strip().lower()
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
    chinese_address: str = ""
    referral_code: str = "NTKWJX"
    freecard_referrer: str = "447942946765"
    coupon_code: str = "DEAL50OFF"
    expected_price_gbp: str = "5.95"


@dataclass
class AppConfig:
    server_url: str = "https://gg.6667766.xyz"
    app_password: str = ""
    remember_credentials: bool = True
    purchase_route: str = PURCHASE_ROUTE_FREECARD
    continuous_enabled: bool = False
    continuous_count: int = 100
    continuous_interval_seconds: int = 3
    application_url: str = DEFAULT_APPLICATION_URL
    browser_channel: str = "msedge"
    user_data_dir: str = field(default_factory=default_user_data_dir)
    headless: bool = False
    slow_mo_ms: int = 800
    page_timeout_ms: int = 120000
    step_timeout_ms: int = 20000
    verification_min_wait_seconds: int = 8
    verification_timeout_seconds: int = 180
    payment_timeout_seconds: int = 1800
    order_sync_timeout_seconds: int = 180
    error_browser_hold_seconds: int = 180
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    registration: RegistrationDefaults = field(default_factory=RegistrationDefaults)


def _merge_config(raw: dict[str, Any]) -> AppConfig:
    proxy_raw = raw.get("proxy") if isinstance(raw.get("proxy"), dict) else {}
    proxy_values = {
        key: value
        for key, value in proxy_raw.items()
        if key in ProxyConfig.__dataclass_fields__
        and key != "password"
    }
    proxy_values["password"] = unprotect_secret(
        str(proxy_raw.get("password_protected") or "")
    )
    # 兼容曾经写入明文 password 的内部开发配置，下一次保存会自动迁移。
    if not proxy_values["password"]:
        proxy_values["password"] = str(proxy_raw.get("password") or "")
    proxy = ProxyConfig(**proxy_values)
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
    values = {
        key: value
        for key, value in raw.items()
        if key in AppConfig.__dataclass_fields__
        and key not in {"proxy", "registration", "app_password"}
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
        values["continuous_interval_seconds"] = min(
            60,
            max(0, int(values.get("continuous_interval_seconds", 3))),
        )
    except (TypeError, ValueError):
        values["continuous_interval_seconds"] = 3
    values["app_password"] = unprotect_secret(
        str(raw.get("app_password_protected") or "")
    )
    return AppConfig(
        **values,
        proxy=proxy,
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
    if config.remember_credentials:
        raw["app_password_protected"] = protect_secret(config.app_password)
        proxy_raw["password_protected"] = protect_secret(config.proxy.password)
    else:
        raw["app_password_protected"] = ""
        proxy_raw["password_protected"] = ""
    target.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
