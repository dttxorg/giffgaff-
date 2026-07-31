from fastapi import FastAPI, HTTPException, UploadFile, File, Request, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse, HTMLResponse
import asyncio
import contextlib
import os
import json
import datetime
import aiosqlite
import httpx
import hmac
import hashlib
import html
import logging
import re
import secrets
import string
import threading
import time
import ipaddress
from collections import deque
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Optional

from database import init_db, DATABASE_PATH
from models import (
    CustomerCreate, CustomerUpdate, CustomerOut, CustomerDetail,
    SystemSettings, AuthLoginRequest, MoEmailCreateRequest,
    SimCodeImport, SimCodeUpdate, SimCodeOut, ActivationStatusUpdate,
    VerificationCodeOut, PaymentInfoEmailOut, CTExcelOrderInfoOut,
    CTExcelClientCustomerCreate, CTExcelPaymentCheckpointRequest,
    InboxMessageSummaryOut, InboxMessageListOut, InboxMessageDetailOut,
    DomainInfo, LabelConfig, EsimCodeUpdate,
    EmailProviderCreate, EmailProviderOut, EmailProviderUpdate,
    ResetCustomerRequest, EmailProviderDomainPick,
)
from crud import (
    get_all_customers, get_customer, search_customers,
    update_customer, delete_customer,
    update_customer_moemail,
    regenerate_public_link, ensure_public_link, bump_all_public_versions,
    save_payment_check_result, save_ctexcel_order_info,
    save_ctexcel_payment_checkpoint,
    regenerate_identity,
    get_public_email,
    get_settings, set_setting, fetch_one, normalize_optional_text
)
from qr_utils import parse_esim_raw, build_lpa_string, generate_esim_qr_png
from email_providers.pool import (
    pick_provider,
    record_provider_use,
    persist_provider_jwt,
    list_providers,
    get_provider,
)
from email_providers.auth import (
    hydrate_provider,
    extract_jwt_for_persist,
)

app = FastAPI(title="giffgaff-label-manager API")

APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()
ADMIN_ENTRY_PATH = os.getenv("ADMIN_ENTRY_PATH", "").strip()
AUTH_COOKIE_NAME = "__Host-giffgaff_label_auth"
ADMIN_ENTRY_COOKIE_NAME = "__Host-giffgaff_admin_entry"
ADMIN_ENTRY_TTL_SECONDS = 12 * 60 * 60
ADMIN_ENTRY_CLOCK_SKEW_SECONDS = 5 * 60
LOGIN_FAILURE_WINDOW_SECONDS = 10 * 60
LOGIN_FAILURE_LIMIT = 5
_LOGIN_FAILURES: dict[str, deque[float]] = {}
_LOGIN_FAILURES_LOCK = threading.Lock()
DEFAULT_GIFFGAFF_DOWNLOAD_URL = "https://www.giffgaff.com/mobile-app"
DEFAULT_ACTIVATION_TUTORIAL_URL = "https://gg.681218.xyz/activation.html"
DEFAULT_ACTIVATION_PAGE_VERSION = 1
DEFAULT_PHONE_STATUS = "激活"
DEFAULT_APP_MODE = "giffgaff"
PRODUCT_TYPES = {"giffgaff", "ctexcel"}
PHONE_STATUSES = {"激活", "封号", "投诉", "退款", "丢失", "作废"}
ACTIVATION_STATUSES = {
    "未开始", "已分配激活码", "激活中",
    "等待人工支付", "等待转 eSIM", "已完成", "失败",
}
SIM_CODE_STATUSES = {"未分配", "已分配", "激活中", "已使用", "失败", "作废"}
DETACHABLE_ACTIVATION_STATUSES = {"未开始", "已分配激活码", "失败"}


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning(
            "%s is not an integer; using %s",
            name,
            default,
        )
        return default


CTEXCEL_AUTO_SYNC_INTERVAL_SECONDS = _env_int(
    "CTEXCEL_AUTO_SYNC_INTERVAL_SECONDS", 60, 0
)
CTEXCEL_AUTO_SYNC_LOOKBACK_DAYS = _env_int(
    "CTEXCEL_AUTO_SYNC_LOOKBACK_DAYS", 14, 1
)
CTEXCEL_AUTO_SYNC_BATCH_SIZE = _env_int(
    "CTEXCEL_AUTO_SYNC_BATCH_SIZE", 6, 1
)
_CTEXCEL_AUTO_SYNC_TASK: Optional[asyncio.Task] = None
DEFAULT_LABEL_TEMPLATES = [
    {
        "id": "basic-50x30",
        "name": "基础标签 50x30",
        "width_mm": 50,
        "height_mm": 30,
        "elements": [
            {"id": "phone", "type": "text", "source": "手机号", "text": "", "x": 3, "y": 3, "w": 30, "h": 6, "fontSize": 12, "bold": True},
            {"id": "email", "type": "text", "source": "邮箱", "text": "", "x": 3, "y": 10, "w": 31, "h": 6, "fontSize": 6, "bold": False},
            {"id": "mailqr", "type": "qr", "source": "邮箱二维码", "text": "", "x": 36, "y": 3, "w": 11, "h": 11, "fontSize": 8, "bold": False},
            {"id": "appqr", "type": "qr", "source": "Giffgaff下载二维码", "text": "", "x": 37, "y": 17, "w": 9, "h": 9, "fontSize": 8, "bold": False},
            {"id": "apptext", "type": "text", "source": "固定文字", "text": "giffgaff app", "x": 34, "y": 26, "w": 14, "h": 3, "fontSize": 4, "bold": False},
        ],
    },
    {
        "id": "full-50x40",
        "name": "完整标签 50x40",
        "width_mm": 50,
        "height_mm": 40,
        "elements": [
            {"id": "title", "type": "text", "source": "固定文字", "text": "giffgaff SIM", "x": 3, "y": 3, "w": 27, "h": 5, "fontSize": 9, "bold": True},
            {"id": "phone", "type": "text", "source": "手机号", "text": "", "x": 3, "y": 9, "w": 30, "h": 6, "fontSize": 11, "bold": True},
            {"id": "email", "type": "text", "source": "邮箱", "text": "", "x": 3, "y": 17, "w": 31, "h": 7, "fontSize": 6, "bold": False},
            {"id": "date", "type": "text", "source": "开通日期", "text": "", "x": 3, "y": 26, "w": 24, "h": 4, "fontSize": 6, "bold": False},
            {"id": "mailqr", "type": "qr", "source": "邮箱二维码", "text": "", "x": 35, "y": 3, "w": 12, "h": 12, "fontSize": 8, "bold": False},
            {"id": "appqr", "type": "qr", "source": "Giffgaff下载二维码", "text": "", "x": 35, "y": 22, "w": 12, "h": 12, "fontSize": 8, "bold": False},
            {"id": "apptext", "type": "text", "source": "固定文字", "text": "下载 App", "x": 35, "y": 35, "w": 12, "h": 3, "fontSize": 5, "bold": False},
        ],
    },
    {
        "id": "qr-50x40",
        "name": "双码标签 50x40",
        "width_mm": 50,
        "height_mm": 40,
        "elements": [
            {"id": "mailtitle", "type": "text", "source": "固定文字", "text": "邮箱 / 收件箱", "x": 4, "y": 3, "w": 18, "h": 4, "fontSize": 6, "bold": True},
            {"id": "mailqr", "type": "qr", "source": "邮箱二维码", "text": "", "x": 5, "y": 8, "w": 16, "h": 16, "fontSize": 8, "bold": False},
            {"id": "apptitle", "type": "text", "source": "固定文字", "text": "giffgaff App", "x": 28, "y": 3, "w": 18, "h": 4, "fontSize": 6, "bold": True},
            {"id": "appqr", "type": "qr", "source": "Giffgaff下载二维码", "text": "", "x": 29, "y": 8, "w": 16, "h": 16, "fontSize": 8, "bold": False},
            {"id": "phone", "type": "text", "source": "手机号", "text": "", "x": 4, "y": 28, "w": 42, "h": 5, "fontSize": 9, "bold": True},
            {"id": "email", "type": "text", "source": "邮箱", "text": "", "x": 4, "y": 34, "w": 42, "h": 4, "fontSize": 5, "bold": False},
        ],
    },
    {
        "id": "activation-guide-50x40",
        "name": "未激活卡教程 50x40",
        "width_mm": 50,
        "height_mm": 40,
        "elements": [
            {"id": "activation-url", "type": "text", "source": "固定文字", "text": "giffgaff 12 步激活教程", "x": 3, "y": 3, "w": 44, "h": 6, "fontSize": 8, "bold": True},
            {"id": "activation-qr", "type": "qr", "source": "激活教程二维码", "text": "", "x": 13, "y": 9, "w": 24, "h": 24, "fontSize": 8, "bold": False},
            {"id": "activation-tip", "type": "text", "source": "固定文字", "text": "扫码后直接查看，无需二次跳转", "x": 5, "y": 34, "w": 40, "h": 4, "fontSize": 6, "bold": True},
        ],
    },
    {
        "id": "ctexcel-50x40",
        "name": "CTExcel 号码资料 50x40",
        "width_mm": 50,
        "height_mm": 40,
        "elements": [
            {"id": "ctexcel-title", "type": "text", "source": "固定文字", "text": "CTExcel 号码资料", "x": 3, "y": 3, "w": 30, "h": 5, "fontSize": 8, "bold": True},
            {"id": "ctexcel-phone", "type": "text", "source": "手机号", "text": "", "x": 3, "y": 10, "w": 31, "h": 6, "fontSize": 11, "bold": True},
            {"id": "ctexcel-email", "type": "text", "source": "邮箱", "text": "", "x": 3, "y": 18, "w": 31, "h": 6, "fontSize": 6, "bold": False},
            {"id": "ctexcel-order", "type": "text", "source": "CTExcel订单号", "text": "", "x": 3, "y": 27, "w": 31, "h": 7, "fontSize": 5, "bold": True},
            {"id": "ctexcel-public-qr", "type": "qr", "source": "号码资料二维码", "text": "", "x": 35, "y": 4, "w": 12, "h": 12, "fontSize": 8, "bold": False},
            {"id": "ctexcel-qr-hint", "type": "text", "source": "固定文字", "text": "扫码查看号码和订单", "x": 34, "y": 17, "w": 14, "h": 5, "fontSize": 5, "bold": True},
            {"id": "ctexcel-referral", "type": "text", "source": "CTExcel推荐码", "text": "", "x": 34, "y": 26, "w": 14, "h": 7, "fontSize": 6, "bold": True},
        ],
    },
    {
        "id": "courier-50x40",
        "name": "快递单 50x40",
        "width_mm": 50,
        "height_mm": 40,
        "elements": [
            {"id": "courier-title", "type": "text", "source": "固定文字", "text": "收件信息", "x": 3, "y": 3, "w": 18, "h": 5, "fontSize": 8, "bold": True},
            {"id": "courier-company", "type": "text", "source": "快递公司", "text": "", "x": 25, "y": 3, "w": 22, "h": 5, "fontSize": 7, "bold": True},
            {"id": "courier-tracking", "type": "text", "source": "快递单号", "text": "", "x": 3, "y": 9, "w": 44, "h": 6, "fontSize": 9, "bold": True},
            {"id": "courier-address", "type": "text", "source": "收货地址", "text": "", "x": 3, "y": 16, "w": 44, "h": 13, "fontSize": 7, "bold": True},
            {"id": "courier-phone-label", "type": "text", "source": "固定文字", "text": "SIM", "x": 3, "y": 30, "w": 7, "h": 4, "fontSize": 6, "bold": True},
            {"id": "courier-phone", "type": "text", "source": "手机号", "text": "", "x": 11, "y": 29, "w": 36, "h": 6, "fontSize": 9, "bold": True},
            {"id": "courier-date", "type": "text", "source": "开通日期", "text": "", "x": 23, "y": 35, "w": 24, "h": 4, "fontSize": 5, "bold": False},
        ],
    },
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


def _auth_enabled() -> bool:
    return bool(APP_PASSWORD)


def _auth_token() -> str:
    return hmac.new(APP_PASSWORD.encode("utf-8"), b"giffgaff-label-manager", hashlib.sha256).hexdigest()


def _is_authenticated(request: Request) -> bool:
    if not _auth_enabled():
        return True
    cookie = request.cookies.get(AUTH_COOKIE_NAME, "")
    return hmac.compare_digest(cookie, _auth_token())


def _validated_admin_entry_path() -> str:
    """返回规范化后的隐藏入口；空值表示本地开发时不启用入口门禁。"""
    value = (ADMIN_ENTRY_PATH or "").strip()
    if not value:
        return ""
    # 只接受单段、至少 32 位的 URL-safe 随机路径，避免误配为 /admin 等弱入口。
    if not re.fullmatch(r"/[A-Za-z0-9_-]{32,128}", value):
        raise ValueError(
            "ADMIN_ENTRY_PATH must be one URL-safe path segment with at least 32 characters"
        )
    return value


def _validate_admin_entry_config() -> str:
    path = _validated_admin_entry_path()
    if path and not APP_PASSWORD:
        raise ValueError("APP_PASSWORD is required when ADMIN_ENTRY_PATH is configured")
    return path


def _admin_entry_signing_key(path: str) -> bytes:
    material = f"{path}\0{APP_PASSWORD}".encode("utf-8")
    return hashlib.sha256(material).digest()


def _new_admin_entry_cookie(path: str, issued_at: Optional[int] = None) -> str:
    issued_at = int(time.time()) if issued_at is None else int(issued_at)
    nonce = secrets.token_urlsafe(32)
    payload = f"v1.{issued_at}.{nonce}"
    signature = hmac.new(
        _admin_entry_signing_key(path),
        payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{signature}"


def _has_admin_entry_cookie(
    request: Request,
    path: str,
    now: Optional[int] = None,
) -> bool:
    value = request.cookies.get(ADMIN_ENTRY_COOKIE_NAME, "")
    if not value or len(value) > 320:
        return False
    parts = value.split(".")
    if len(parts) != 4:
        return False
    version, issued_at_raw, nonce, supplied_signature = parts
    if version != "v1" or not issued_at_raw.isdigit():
        return False
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", nonce):
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", supplied_signature):
        return False
    payload = f"{version}.{issued_at_raw}.{nonce}"
    expected_signature = hmac.new(
        _admin_entry_signing_key(path),
        payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        return False

    issued_at = int(issued_at_raw)
    current = int(time.time()) if now is None else int(now)
    if issued_at > current + ADMIN_ENTRY_CLOCK_SKEW_SECONDS:
        return False
    return current - issued_at < ADMIN_ENTRY_TTL_SECONDS


def _login_client_ip(request: Request) -> str:
    """优先使用 Cloudflare 写入的真实客户端 IP，否则使用 ASGI client。"""
    cf_ip = (request.headers.get("CF-Connecting-IP") or "").strip()
    if cf_ip:
        try:
            return str(ipaddress.ip_address(cf_ip))
        except ValueError:
            pass
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _register_login_failure(client_ip: str, now: Optional[float] = None) -> tuple[bool, int]:
    """登记一次失败；返回 (是否仍允许普通 401, Retry-After 秒数)。"""
    current = time.monotonic() if now is None else float(now)
    cutoff = current - LOGIN_FAILURE_WINDOW_SECONDS
    with _LOGIN_FAILURES_LOCK:
        attempts = _LOGIN_FAILURES.setdefault(client_ip, deque())
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if not attempts:
            _LOGIN_FAILURES[client_ip] = attempts
        if len(attempts) >= LOGIN_FAILURE_LIMIT:
            retry_after = max(1, int(LOGIN_FAILURE_WINDOW_SECONDS - (current - attempts[0])) + 1)
            return False, retry_after
        attempts.append(current)
        return True, 0


def _clear_login_failures(client_ip: str) -> None:
    with _LOGIN_FAILURES_LOCK:
        _LOGIN_FAILURES.pop(client_ip, None)


def _reset_login_failure_state() -> None:
    """测试和进程维护使用；不暴露为 HTTP 接口。"""
    with _LOGIN_FAILURES_LOCK:
        _LOGIN_FAILURES.clear()


def _require_ctexcel_client(request: Request) -> None:
    """验证仅供 CTExcel 桌面申请流程使用的限权 API。"""
    if not _auth_enabled():
        return
    authorization = (request.headers.get("Authorization") or "").strip()
    expected = f"Bearer {APP_PASSWORD}"
    failure_key = f"ctexcel-client:{_login_client_ip(request)}"
    if not hmac.compare_digest(authorization, expected):
        allowed, retry_after = _register_login_failure(failure_key)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="客户端连接失败次数过多，请稍后再试",
                headers={"Retry-After": str(retry_after)},
            )
        raise HTTPException(status_code=401, detail="客户端连接口令错误")
    _clear_login_failures(failure_key)


def _hidden_admin_not_found() -> Response:
    return Response(
        content="Not found",
        status_code=404,
        media_type="text/plain",
        headers={
            "Cache-Control": "no-store, max-age=0",
            "X-Content-Type-Options": "nosniff",
            "X-Robots-Tag": "noindex, nofollow, noarchive",
        },
    )


def _admin_config_error() -> Response:
    return Response(
        content="Server configuration error",
        status_code=500,
        media_type="text/plain",
        headers={
            "Cache-Control": "no-store, max-age=0",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _normalize_base_url(value: Optional[str]) -> str:
    return (value or "").strip().rstrip("/")


def _normalize_share_link(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    return value.strip().replace("//shared/", "/shared/")


def _customer_payload(row) -> dict:
    customer = dict(row)
    customer["share_link"] = _normalize_share_link(customer.get("share_link"))
    customer["phone_status"] = _normalize_phone_status(customer.get("phone_status"))
    customer["activation_status"] = _normalize_activation_status(customer.get("activation_status"))
    customer.pop("initial_password", None)
    customer.pop("automation_lock_owner", None)
    customer.pop("automation_locked_at", None)
    customer.pop("ctexcel_client_request_key", None)
    return customer


def _normalize_phone_status(value: Optional[str]) -> str:
    value = (value or "").strip()
    return value if value in PHONE_STATUSES else DEFAULT_PHONE_STATUS


def _normalize_product_type(value: Optional[str]) -> str:
    value = (value or "").strip().lower()
    return value if value in PRODUCT_TYPES else DEFAULT_APP_MODE


def _normalize_activation_status(value: Optional[str]) -> str:
    value = (value or "").strip()
    if value == "等待客户端领取":
        return "已分配激活码"
    return value if value in ACTIVATION_STATUSES else "未开始"


def _normalize_sim_code_status(value: Optional[str]) -> str:
    value = (value or "").strip()
    return value if value in SIM_CODE_STATUSES else "未分配"


def _normalize_sim_code(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _generate_initial_password() -> str:
    alphabet = string.ascii_uppercase + string.ascii_lowercase + string.digits
    random_part = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"Gg-{random_part}!"


def _utc_now() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _masked_setting(rows: dict, key: str) -> str:
    return "***" if rows.get(key) else ""


def _first_text(data: dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return str(value)
    return ""


def _message_list(payload) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("messages", "data", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    email = payload.get("email")
    if isinstance(email, dict) and isinstance(email.get("messages"), list):
        return [item for item in email["messages"] if isinstance(item, dict)]
    return []


def _message_id(message: dict) -> str:
    return _first_text(message, "id", "messageId", "message_id")


def _normalize_message_timestamp(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        try:
            numeric = float(text)
            magnitude = abs(numeric)
            if magnitude >= 1e14:
                seconds = numeric / 1_000_000
            elif magnitude >= 1e11:
                seconds = numeric / 1_000
            else:
                seconds = numeric
            parsed = datetime.datetime.fromtimestamp(
                seconds,
                tz=datetime.timezone.utc,
            )
            return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return text
    return text


def _message_received_at(message: dict) -> str:
    return _normalize_message_timestamp(
        _first_text(message, "receivedAt", "received_at", "createdAt", "created_at", "date")
    )


def _message_header_value(message: dict, wanted: str) -> str:
    headers = message.get("headers")
    wanted_lower = wanted.lower()
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() != wanted_lower:
                continue
            if isinstance(value, list):
                return ", ".join(str(item) for item in value if item is not None)
            return str(value or "")
    if isinstance(headers, list):
        for item in headers:
            if not isinstance(item, dict):
                continue
            name = _first_text(item, "name", "key").lower()
            if name == wanted_lower:
                return _first_text(item, "value", "text")
    return ""


def _message_sent_at(message: dict) -> str:
    direct = _first_text(
        message,
        "sentAt",
        "sent_at",
        "sentDate",
        "sent_date",
        "dateSent",
        "date_sent",
    )
    return _normalize_message_timestamp(
        direct or _message_header_value(message, "date") or _first_text(message, "date")
    )


def _message_detail_payload(payload) -> dict:
    if not isinstance(payload, dict):
        return {}
    for key in ("message", "data", "item"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return payload


def _message_address(message: dict, *keys: str) -> str:
    for key in keys:
        value = message.get(key)
        if isinstance(value, dict):
            nested = _first_text(value, "address", "email", "value", "name")
            if nested:
                return nested
        elif isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, dict):
                    part = _first_text(item, "address", "email", "value", "name")
                else:
                    part = str(item or "")
                if part:
                    parts.append(part)
            if parts:
                return ", ".join(parts)
        elif value is not None:
            return str(value)
    return ""


def _plain_text_from_html(value: str) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<br\s*/?>", "\n", text)
    text = re.sub(r"(?s)</p\s*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text)


def _looks_like_html(value: str) -> bool:
    return bool(
        re.search(
            r"(?is)<(?:!doctype|html|body|div|p|br|table|a|img|span)\b",
            value or "",
        )
    )


def _message_body_text(message: dict) -> str:
    plain = _first_text(
        message,
        "text",
        "content",
        "body",
        "plainText",
        "plain_text",
        "textContent",
        "text_content",
    ).strip()
    if plain:
        if _looks_like_html(plain):
            rendered = _plain_text_from_html(plain)
            rendered = re.sub(r"\n[ \t]+", "\n", rendered)
            rendered = re.sub(r"\n{3,}", "\n\n", rendered)
            return rendered.strip()
        return plain
    html_body = _first_text(
        message,
        "html",
        "htmlContent",
        "html_content",
        "htmlBody",
        "html_body",
    )
    if not html_body:
        return ""
    rendered = _plain_text_from_html(html_body)
    rendered = re.sub(r"\n[ \t]+", "\n", rendered)
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    return rendered.strip()


def _message_html_body(message: dict) -> str:
    explicit = _first_text(
        message,
        "html",
        "htmlContent",
        "html_content",
        "htmlBody",
        "html_body",
    )
    if explicit:
        return explicit
    fallback = _first_text(
        message,
        "text",
        "content",
        "body",
        "textContent",
        "text_content",
    )
    return fallback if _looks_like_html(fallback) else ""


def _extract_verification_code(message: dict) -> Optional[str]:
    subject = _first_text(message, "subject")
    content = _first_text(message, "content", "text", "body", "plainText", "plain_text")
    html_content = _plain_text_from_html(
        _first_text(message, "html", "htmlContent", "html_content", "htmlBody", "html_body")
    )
    text = "\n".join(part for part in (subject, content, html_content) if part)
    if not text:
        return None
    patterns = (
        r"(?is)verification\s+code\s*(?:is)?\s*[:：]?\s*(\d{6})",
        r"(?is)code\s*(?:is)?\s*[:：]?\s*(\d{6})",
        r"(?is)验证码\s*(?:是|为)?\s*[:：]?\s*(\d{6})",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    match = re.search(r"(?<!\d)\d{6}(?!\d)", text)
    return match.group(0) if match else None


def _message_search_text(message: dict) -> str:
    subject = _first_text(message, "subject")
    content = _first_text(message, "content", "text", "body", "plainText", "plain_text")
    html_content = _plain_text_from_html(
        _first_text(message, "html", "htmlContent", "html_content", "htmlBody", "html_body")
    )
    return "\n".join(part for part in (subject, content, html_content) if part)


def _payment_info_email_kind(message: dict) -> Optional[str]:
    text = _message_search_text(message)
    if re.search(r"payment\s+info\s+has\s+changed", text, re.I):
        return "changed"
    if re.search(r"payment\s+info\s+has\s+been\s+updated", text, re.I):
        return "updated"
    return None


def _extract_ctexcel_order_info(message: dict) -> dict:
    """从 CTExcel 订单邮件的纯文本或 HTML 正文中提取关键资料。"""
    text = _message_search_text(message)
    if not text:
        return {}
    normalized = html.unescape(text).replace("\u00a0", " ")
    # 邮件正文有时会保留 Markdown 的 **粗体** 标记；字段解析不依赖排版符号。
    normalized = re.sub(r"\*+", "", normalized)

    def find(pattern: str, flags: int = re.I) -> Optional[str]:
        match = re.search(pattern, normalized, flags)
        return match.group(1).strip() if match else None

    order_number = find(
        r"(?:订单号|order\s*(?:number|no\.?))\s*[:：]\s*\**\s*([A-Z0-9][A-Z0-9-]{7,})"
    )
    phone_number = find(
        r"(?:手机号码|电话号码|mobile\s*(?:number|no\.?))\s*[:：]\s*\**\s*((?:\+?44|0)7\d{9})"
    )
    transaction_amount = find(
        r"(?:交易金额|订单金额|付款金额|支付金额|预存金额|"
        r"transaction\s*amount|payment\s*amount)"
        r"\s*[:：]\s*\**\s*[£￡]?\s*([0-9]+(?:\.[0-9]{1,2})?)"
    )
    referral_code = find(
        r"(?:专属推荐码|推荐码|referral\s*code)\s*[:：]\s*\**\s*([A-Z0-9]{4,20})"
    )
    referral_link = find(
        r"(https?://(?:www\.)?ctexcel\.com/[^\s<>\])\"']+recommendCode=[A-Z0-9]+)"
    )
    if referral_link:
        referral_link = referral_link.rstrip(".,;，。；")
    return {
        "phone_number": phone_number,
        "order_number": order_number,
        "transaction_amount": transaction_amount,
        "referral_code": referral_code,
        "referral_link": referral_link,
    }


def _is_ctexcel_registration_confirmation(message: dict) -> bool:
    """识别 £1 领卡流程的订单确认主题，避免重复提交同一邮箱。"""
    subject = html.unescape(
        _first_text(message, "subject") or ""
    )
    normalized = re.sub(r"\s+", "", subject).casefold()
    return (
        "ctexcel" in normalized
        and "您的订单已确认" in normalized
    )


def _merge_default_label_templates(templates: list[dict]) -> list[dict]:
    merged = deepcopy(templates)
    existing_ids = {tpl.get("id") for tpl in merged if isinstance(tpl, dict)}
    for template in DEFAULT_LABEL_TEMPLATES:
        if template["id"] not in existing_ids:
            merged.append(deepcopy(template))
    return merged


async def _claim_pending_ctexcel_auto_sync_customers() -> list[dict]:
    """跨 Uvicorn worker 抢占一批待扫描的 CTExcel 邮箱。"""
    retry_seconds = max(30, CTEXCEL_AUTO_SYNC_INTERVAL_SECONDS)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        # BEGIN IMMEDIATE 让多个 worker 串行执行“选择 + 标记”，避免重复回源。
        await db.execute("BEGIN IMMEDIATE")
        rows = await db.execute_fetchall(
            """SELECT *
               FROM customers
               WHERE product_type = 'ctexcel'
                 AND NULLIF(
                       TRIM(ctexcel_registration_confirmed_at), ''
                     ) IS NULL
                 AND (
                       NULLIF(TRIM(phone_number), '') IS NULL
                       OR NULLIF(TRIM(ctexcel_order_number), '') IS NULL
                     )
                 AND (
                       NULLIF(TRIM(email_account_id), '') IS NOT NULL
                       OR NULLIF(TRIM(moemail_id), '') IS NOT NULL
                     )
                 AND datetime(created_at) >= datetime('now', ?)
                 AND (
                       ctexcel_last_checked_at IS NULL
                       OR datetime(ctexcel_last_checked_at) <= datetime('now', ?)
                     )
               ORDER BY
                 CASE WHEN ctexcel_last_checked_at IS NULL THEN 0 ELSE 1 END,
                 ctexcel_last_checked_at ASC,
                 id ASC
               LIMIT ?""",
            (
                f"-{CTEXCEL_AUTO_SYNC_LOOKBACK_DAYS} days",
                f"-{retry_seconds} seconds",
                CTEXCEL_AUTO_SYNC_BATCH_SIZE,
            ),
        )
        claimed = [dict(row) for row in rows]
        if claimed:
            customer_ids = [int(row["id"]) for row in claimed]
            placeholders = ",".join("?" for _ in customer_ids)
            await db.execute(
                f"""UPDATE customers
                    SET ctexcel_last_checked_at = ?
                    WHERE id IN ({placeholders})""",
                (_utc_now(), *customer_ids),
            )
        await db.commit()
        return claimed


async def _ctexcel_auto_sync_once() -> dict[str, int]:
    rows = await _claim_pending_ctexcel_auto_sync_customers()
    result = {"checked": 0, "synced": 0, "failed": 0}
    for customer in rows:
        result["checked"] += 1
        try:
            synced = await _sync_ctexcel_order_info(customer, limit=50)
            if synced.found:
                result["synced"] += 1
        except Exception as exc:
            # 单个邮箱异常留到下一轮重试，不影响其他客户。
            result["failed"] += 1
            logging.getLogger(__name__).warning(
                "CTExcel mailbox auto-sync failed for customer %s: %s",
                customer.get("id"),
                exc,
            )
    return result


async def _ctexcel_auto_sync_loop() -> None:
    while True:
        await asyncio.sleep(max(1, CTEXCEL_AUTO_SYNC_INTERVAL_SECONDS))
        try:
            await _ctexcel_auto_sync_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).exception(
                "CTExcel mailbox auto-sync round failed"
            )


@app.middleware("http")
async def require_app_password(request, call_next):
    path = request.url.path

    # 二维码公开页不依赖隐藏管理入口，供 Cloudflare Worker 和扫码用户访问。
    entry_gate_exempt = (
        path.startswith("/p/")
        or path.startswith("/api/public/")
        or path.startswith("/api/ctexcel-client/")
    )
    if not entry_gate_exempt:
        try:
            admin_entry_path = _validate_admin_entry_config()
        except ValueError:
            return _admin_config_error()

        if admin_entry_path:
            if request.method == "GET" and path == admin_entry_path:
                response = RedirectResponse(url="/index.html", status_code=302)
                response.set_cookie(
                    ADMIN_ENTRY_COOKIE_NAME,
                    _new_admin_entry_cookie(admin_entry_path),
                    path="/",
                    httponly=True,
                    secure=True,
                    samesite="lax",
                    max_age=ADMIN_ENTRY_TTL_SECONDS,
                    expires=(
                        datetime.datetime.now(datetime.timezone.utc)
                        + datetime.timedelta(seconds=ADMIN_ENTRY_TTL_SECONDS)
                    ),
                )
                response.headers["Cache-Control"] = "no-store, max-age=0"
                response.headers["Referrer-Policy"] = "no-referrer"
                return response
            if not _has_admin_entry_cookie(request, admin_entry_path):
                return _hidden_admin_not_found()

    public_paths = {"/api/auth/status", "/api/auth/login", "/api/auth/logout"}
    protected_prefixes = ("/api", "/docs", "/redoc", "/openapi.json")
    # /api/public/* 是 Cloudflare Worker 在边缘节点回调的，绕过后台口令鉴权
    if path.startswith("/api/public/"):
        return await call_next(request)
    # CTExcel 桌面客户端使用独立的 Bearer 口令鉴权，不依赖浏览器 Cookie。
    if path.startswith("/api/ctexcel-client/"):
        return await call_next(request)
    if _auth_enabled() and path not in public_paths and path.startswith(protected_prefixes):
        if not _is_authenticated(request):
            return JSONResponse({"detail": "需要登录"}, status_code=401)
    return await call_next(request)


@app.on_event("startup")
async def startup():
    global _CTEXCEL_AUTO_SYNC_TASK
    _validate_admin_entry_config()
    await init_db()
    if (
        CTEXCEL_AUTO_SYNC_INTERVAL_SECONDS > 0
        and (
            _CTEXCEL_AUTO_SYNC_TASK is None
            or _CTEXCEL_AUTO_SYNC_TASK.done()
        )
    ):
        _CTEXCEL_AUTO_SYNC_TASK = asyncio.create_task(
            _ctexcel_auto_sync_loop(),
            name="ctexcel-auto-email-sync",
        )


@app.on_event("shutdown")
async def shutdown():
    global _CTEXCEL_AUTO_SYNC_TASK
    if _CTEXCEL_AUTO_SYNC_TASK:
        _CTEXCEL_AUTO_SYNC_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _CTEXCEL_AUTO_SYNC_TASK
        _CTEXCEL_AUTO_SYNC_TASK = None


# ── 访问口令 ──

@app.get("/api/auth/status")
async def auth_status(request: Request):
    return {
        "auth_required": _auth_enabled(),
        "authenticated": _is_authenticated(request),
    }


@app.post("/api/auth/login")
async def auth_login(data: AuthLoginRequest, request: Request):
    if not _auth_enabled():
        return {"ok": True}
    client_ip = _login_client_ip(request)
    if not hmac.compare_digest(data.password, APP_PASSWORD):
        allowed, retry_after = _register_login_failure(client_ip)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="登录失败次数过多，请稍后再试",
                headers={"Retry-After": str(retry_after)},
            )
        raise HTTPException(status_code=401, detail="口令错误")
    _clear_login_failures(client_ip)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        AUTH_COOKIE_NAME,
        _auth_token(),
        path="/",
        httponly=True,
        samesite="lax",
        secure=True,
    )
    return response


@app.post("/api/auth/logout")
async def auth_logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(
        AUTH_COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response


# ── 系统设置 ──

def _activation_page_version(settings: dict) -> int:
    try:
        return max(1, int(settings.get("activation_page_version") or 1))
    except (TypeError, ValueError):
        return DEFAULT_ACTIVATION_PAGE_VERSION


async def _bump_activation_page_version(settings: dict) -> int:
    version = _activation_page_version(settings) + 1
    await set_setting("activation_page_version", str(version))
    return version

@app.get("/api/settings", response_model=SystemSettings)
async def get_sys_settings():
    rows = await get_settings()
    return SystemSettings(
        app_mode=_normalize_product_type(rows.get("app_mode")),
        giffgaff_download_url=rows.get("giffgaff_download_url", DEFAULT_GIFFGAFF_DOWNLOAD_URL),
        activation_tutorial_url=rows.get(
            "activation_tutorial_url", DEFAULT_ACTIVATION_TUTORIAL_URL
        ),
        activation_page_markdown=rows.get("activation_page_markdown", ""),
        activation_page_version=_activation_page_version(rows),
        public_page_markdown=rows.get("public_page_markdown", ""),
        public_worker_domain=rows.get("public_worker_domain"),
        custom_public_vars=rows.get("custom_public_vars", "") or "",
    )


@app.patch("/api/settings")
async def update_settings(data: SystemSettings):
    rows = await get_settings()
    activation_page_changed = False
    contact_page_changed = False
    if data.app_mode is not None:
        await set_setting("app_mode", _normalize_product_type(data.app_mode))
    if data.giffgaff_download_url is not None:
        await set_setting("giffgaff_download_url", data.giffgaff_download_url)
    if data.activation_tutorial_url is not None:
        tutorial_url = data.activation_tutorial_url.strip() or DEFAULT_ACTIVATION_TUTORIAL_URL
        if not tutorial_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="激活教程地址必须以 http:// 或 https:// 开头")
        activation_page_changed = rows.get("activation_tutorial_url", DEFAULT_ACTIVATION_TUTORIAL_URL) != tutorial_url
        await set_setting("activation_tutorial_url", tutorial_url)
    if data.activation_page_markdown is not None:
        activation_page_changed = (
            activation_page_changed
            or rows.get("activation_page_markdown", "") != data.activation_page_markdown
        )
        await set_setting("activation_page_markdown", data.activation_page_markdown)
    if data.public_page_markdown is not None:
        contact_page_changed = rows.get("public_page_markdown", "") != data.public_page_markdown
        await set_setting("public_page_markdown", data.public_page_markdown)
    if data.public_worker_domain is not None:
        v = data.public_worker_domain.strip()
        if v and not (v.startswith("http://") or v.startswith("https://")):
            raise HTTPException(status_code=400, detail="Worker 域名必须以 http:// 或 https:// 开头")
        # 去掉尾部斜杠
        v = v.rstrip("/")
        await set_setting("public_worker_domain", v)
    if data.custom_public_vars is not None:
        # 校验是合法 JSON object
        import json as _json
        raw = data.custom_public_vars.strip()
        if raw:
            try:
                parsed = _json.loads(raw)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"自定义变量必须是合法 JSON：{exc}")
            if not isinstance(parsed, dict):
                raise HTTPException(status_code=400, detail="自定义变量必须是 JSON object（key: value 形式）")
            for k in parsed.keys():
                if not isinstance(k, str) or not re.match(r"^[a-zA-Z0-9_]+$", k):
                    raise HTTPException(
                        status_code=400,
                        detail=f"变量名 {k!r} 非法：只允许字母数字下划线",
                    )
        await set_setting("custom_public_vars", raw)
    if activation_page_changed:
        await _bump_activation_page_version(rows)
    if contact_page_changed:
        await bump_all_public_versions()
    return {"ok": True}


async def _get_setting(key: str) -> str:
    rows = await get_settings()
    return rows.get(key, "")


async def _generate_email_account(*, manual_provider_id: Optional[int] = None, manual_domain: Optional[str] = None) -> dict:
    """Pool-backed email account generator.

    Returns {email, email_account_id, email_provider_id, email_provider_domain, share_link, is_email_auto}.
    Raises HTTPException(503) if no usable provider, 502 if generation fails.
    """
    try:
        provider_id, provider = pick_provider(DATABASE_PATH, manual_provider_id=manual_provider_id)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"{exc}。请在网页右上角「邮箱服务商」标签里至少添加一个 provider "
                "(MoEmail 或 Cloud-Mail)，并配置 URL / API Key。"
            ),
        ) from exc
    try:
        # MoEmailProvider accepts domain= kwarg; CloudMailProvider ignores it
        # because its domain is part of the instance configuration.
        gen = provider.generate_email(domain=manual_domain)
    except Exception as exc:
        record_provider_use(DATABASE_PATH, provider_id, error=str(exc))
        raise HTTPException(status_code=502, detail=f"生成邮箱失败：{exc}") from exc
    # Defer last_used_at/jwt persistence until the downstream INSERT commits.
    # See _record_email_provider_use_after_commit. If the INSERT fails and we
    # skip that call, the email account already exists on the provider but
    # will simply age out (and is harmless).
    jwt, jwt_at = extract_jwt_for_persist(provider)
    return {
        "email": gen.address,
        "email_account_id": gen.provider_account_id,
        "email_provider_id": provider_id,
        "email_provider_domain": manual_domain,
        "share_link": gen.share_link,
        "is_email_auto": True,
        "_provider_jwt": (provider_id, jwt, jwt_at) if jwt else None,
    }


async def _record_email_provider_use_after_commit(payload: Optional[dict]) -> None:
    """Mark a provider as successfully used and persist any JWT it returned.

    Call this ONLY after the customer row that references this email provider
    has been committed. Calling it on a pending or failed transaction will skew
    the pool's round-robin / cooldown signals.
    """
    if not payload:
        return
    provider_id = payload.get("email_provider_id")
    if not provider_id:
        return
    # Look up via the pool module each call so test monkeypatches apply.
    from email_providers import pool as _pool
    try:
        _pool.record_provider_use(DATABASE_PATH, provider_id)
    except Exception:
        # If recording use fails we don't want to roll back the customer insert.
        pass
    jwt_info = payload.get("_provider_jwt")
    if jwt_info:
        _, jwt, jwt_at = jwt_info
        try:
            _pool.persist_provider_jwt(DATABASE_PATH, provider_id, jwt, jwt_at)
        except Exception:
            pass


# Note: legacy `_generate_moemail_account` removed (was just an alias to _generate_email_account).
# Callers updated inline to call `_generate_email_account` directly.


async def _has_available_sim_code() -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        row = await fetch_one(db, "SELECT id FROM sim_codes WHERE status = '未分配' ORDER BY id ASC LIMIT 1")
        return bool(row)


async def _create_customer_without_activation(data: CustomerCreate, email_bundle: dict) -> int:
    phone_number = normalize_optional_text(data.phone_number)
    shipping_address = normalize_optional_text(data.shipping_address)
    courier_company = normalize_optional_text(data.courier_company)
    tracking_number = normalize_optional_text(data.tracking_number)
    courier_order_code = normalize_optional_text(data.courier_order_code)
    courier_print_data = normalize_optional_text(data.courier_print_data)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO customers
               (product_type, phone_number, email, shipping_address, courier_company,
                tracking_number, courier_order_code, courier_print_data, activation_date,
                moemail_id, moemail_address, share_link, is_moemail_auto,
                email_provider_id, email_account_id, email_provider_domain, activation_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _normalize_product_type(data.product_type),
                phone_number,
                email_bundle.get("email", ""),
                shipping_address,
                courier_company,
                tracking_number,
                courier_order_code,
                courier_print_data,
                data.activation_date.isoformat(),
                email_bundle.get("moemail_id"),
                email_bundle.get("moemail_address"),
                email_bundle.get("share_link"),
                1 if email_bundle.get("is_moemail_auto") else 0,
                email_bundle.get("email_provider_id"),
                email_bundle.get("email_account_id"),
                email_bundle.get("email_provider_domain"),
                "未开始",
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def _create_customer_with_activation(data: CustomerCreate, email_bundle: dict, initial_password: str) -> tuple[int, dict]:
    phone_number = normalize_optional_text(data.phone_number)
    shipping_address = normalize_optional_text(data.shipping_address)
    courier_company = normalize_optional_text(data.courier_company)
    tracking_number = normalize_optional_text(data.tracking_number)
    courier_order_code = normalize_optional_text(data.courier_order_code)
    courier_print_data = normalize_optional_text(data.courier_print_data)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("BEGIN IMMEDIATE")
            sim = await fetch_one(
                db,
                "SELECT id, code FROM sim_codes WHERE status = '未分配' ORDER BY id ASC LIMIT 1",
            )
            if not sim:
                raise HTTPException(status_code=400, detail="没有可用 SIM 激活码，请先导入激活码")
            cursor = await db.execute(
                """INSERT INTO customers
                   (product_type, phone_number, email, shipping_address, courier_company,
                    tracking_number, courier_order_code, courier_print_data, activation_date,
                    moemail_id, moemail_address, share_link, is_moemail_auto,
                    sim_code_id, sim_activation_code, initial_password,
                    email_provider_id, email_account_id, email_provider_domain, activation_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _normalize_product_type(data.product_type),
                    phone_number,
                    email_bundle.get("email", ""),
                    shipping_address,
                    courier_company,
                    tracking_number,
                    courier_order_code,
                    courier_print_data,
                    data.activation_date.isoformat(),
                    email_bundle.get("moemail_id"),
                    email_bundle.get("moemail_address"),
                    email_bundle.get("share_link"),
                    1 if email_bundle.get("is_moemail_auto") else 0,
                    sim["id"],
                    sim["code"],
                    initial_password,
                    email_bundle.get("email_provider_id"),
                    email_bundle.get("email_account_id"),
                    email_bundle.get("email_provider_domain"),
                    "已分配激活码",
                ),
            )
            customer_id = cursor.lastrowid
            await db.execute(
                "UPDATE sim_codes SET status = '已分配', customer_id = ?, updated_at = datetime('now') WHERE id = ?",
                (customer_id, sim["id"]),
            )
            await db.execute(
                """INSERT INTO activation_logs (customer_id, level, step, message)
                   VALUES (?, 'info', 'created', ?)""",
                (customer_id, f"已分配 SIM 激活码 {sim['code']}，等待人工激活"),
            )
            await db.commit()
            return customer_id, {"id": sim["id"], "code": sim["code"]}
        except Exception:
            await db.rollback()
            raise


# ── 客户管理 ──

@app.get("/api/customers", response_model=list[CustomerOut])
async def list_customers(search: str = ""):
    rows = await (search_customers(search) if (search or "").strip() else get_all_customers())
    return [CustomerOut(
        id=r["id"],
        product_type=_normalize_product_type(r.get("product_type")),
        phone_number=r["phone_number"],
        email=r["email"],
        shipping_address=r.get("shipping_address"),
        phone_status=_normalize_phone_status(r.get("phone_status")),
        courier_company=r.get("courier_company"),
        tracking_number=r.get("tracking_number"),
        courier_order_code=r.get("courier_order_code"),
        activation_date=r["activation_date"],
        moemail_id=r.get("moemail_id"),
        moemail_address=r.get("moemail_address"),
        email_provider_id=r.get("email_provider_id"),
        email_account_id=r.get("email_account_id"),
        share_link=_normalize_share_link(r.get("share_link")),
        is_moemail_auto=bool(r.get("is_moemail_auto")),
        sim_code_id=r.get("sim_code_id"),
        sim_activation_code=r.get("sim_activation_code"),
        public_token=r.get("public_token"),
        public_version=int(r.get("public_version") or 1),
        first_name=r.get("first_name"),
        last_name=r.get("last_name"),
        address=r.get("address"),
        city=r.get("city"),
        postcode=r.get("postcode"),
        ctexcel_order_number=r.get("ctexcel_order_number"),
        ctexcel_transaction_amount=r.get("ctexcel_transaction_amount"),
        ctexcel_referral_code=r.get("ctexcel_referral_code"),
        ctexcel_referral_link=r.get("ctexcel_referral_link"),
        ctexcel_last_checked_at=r.get("ctexcel_last_checked_at"),
        ctexcel_registration_confirmed_at=r.get(
            "ctexcel_registration_confirmed_at"
        ),
        ctexcel_payment_succeeded_at=r.get(
            "ctexcel_payment_succeeded_at"
        ),
        payment_changed_at=r.get("payment_changed_at"),
        payment_updated_at=r.get("payment_updated_at"),
        payment_last_checked_at=r.get("payment_last_checked_at"),
        esim_raw_code=r.get("esim_raw_code"),
        activation_status=_normalize_activation_status(r.get("activation_status")),
        activation_error=r.get("activation_error"),
        activated_at=r.get("activated_at"),
        created_at=r["created_at"],
    ) for r in rows]


@app.get("/api/customers/{customer_id}", response_model=CustomerDetail)
async def get_customer_detail(customer_id: int):
    c = await get_customer(customer_id)
    if not c:
        raise HTTPException(status_code=404, detail="客户不存在")
    return CustomerDetail(
        id=c["id"],
        product_type=_normalize_product_type(c.get("product_type")),
        phone_number=c["phone_number"],
        email=c["email"],
        shipping_address=c.get("shipping_address"),
        phone_status=_normalize_phone_status(c.get("phone_status")),
        courier_company=c.get("courier_company"),
        tracking_number=c.get("tracking_number"),
        courier_order_code=c.get("courier_order_code"),
        activation_date=c["activation_date"],
        created_at=c["created_at"],
        moemail_id=c.get("moemail_id"),
        moemail_address=c.get("moemail_address"),
        email_provider_id=c.get("email_provider_id"),
        email_account_id=c.get("email_account_id"),
        share_link=_normalize_share_link(c.get("share_link")),
        is_moemail_auto=bool(c.get("is_moemail_auto")),
        sim_code_id=c.get("sim_code_id"),
        sim_activation_code=c.get("sim_activation_code"),
        public_token=c.get("public_token"),
        public_version=int(c.get("public_version") or 1),
        first_name=c.get("first_name"),
        last_name=c.get("last_name"),
        address=c.get("address"),
        city=c.get("city"),
        postcode=c.get("postcode"),
        ctexcel_order_number=c.get("ctexcel_order_number"),
        ctexcel_transaction_amount=c.get("ctexcel_transaction_amount"),
        ctexcel_referral_code=c.get("ctexcel_referral_code"),
        ctexcel_referral_link=c.get("ctexcel_referral_link"),
        ctexcel_last_checked_at=c.get("ctexcel_last_checked_at"),
        ctexcel_registration_confirmed_at=c.get(
            "ctexcel_registration_confirmed_at"
        ),
        ctexcel_payment_succeeded_at=c.get(
            "ctexcel_payment_succeeded_at"
        ),
        payment_changed_at=c.get("payment_changed_at"),
        payment_updated_at=c.get("payment_updated_at"),
        payment_last_checked_at=c.get("payment_last_checked_at"),
        initial_password=c.get("initial_password"),
        esim_raw_code=c.get("esim_raw_code"),
        activation_status=_normalize_activation_status(c.get("activation_status")),
        activation_error=c.get("activation_error"),
        activated_at=c.get("activated_at"),
    )


@app.post("/api/customers", status_code=201)
async def add_customer(data: CustomerCreate):
    product_type = _normalize_product_type(data.product_type)
    use_sim_code = bool(data.use_sim_code and product_type == "giffgaff")
    phone_number = normalize_optional_text(data.phone_number)
    if phone_number:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            db.row_factory = aiosqlite.Row
            existing = await fetch_one(
                db,
                "SELECT id FROM customers WHERE phone_number = ?", (phone_number,)
            )
            if existing:
                raise HTTPException(status_code=409, detail="该手机号已录入")

    if use_sim_code and not await _has_available_sim_code():
        raise HTTPException(status_code=400, detail="没有可用 SIM 激活码，请先导入激活码，或选择不使用激活码")

    try:
        email = (data.email or "").strip()
        if email:
            email_bundle = {"email": email, "is_moemail_auto": False, "email_provider_id": None, "email_account_id": None}
        else:
            email_bundle = await _generate_email_account(
                manual_provider_id=data.email_provider_id,
                manual_domain=data.email_provider_domain,
            )
            # Pool-backed path returns new keys (email_account_id, email_provider_id, share_link)
            # Legacy callers expect moemail_id/moemail_address/is_moemail_auto/share_link.
            email_bundle["moemail_id"] = email_bundle.get("email_account_id")
            email_bundle["moemail_address"] = email_bundle.get("email")
            email_bundle["is_moemail_auto"] = True
        if use_sim_code:
            initial_password = _generate_initial_password()
            customer_id, sim = await _create_customer_with_activation(data, email_bundle, initial_password)
            message = "客户已录入并分配激活码"
            sim_activation_code = sim["code"]
        else:
            initial_password = None
            customer_id = await _create_customer_without_activation(data, email_bundle)
            message = (
                "CTExcel 客户已录入，等待从订单邮件同步号码资料"
                if product_type == "ctexcel"
                else "客户已录入，未使用激活码"
            )
            sim_activation_code = None
    except aiosqlite.IntegrityError:
        raise HTTPException(status_code=409, detail="该手机号已录入")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"自动建档失败：{exc}") from exc

    # Provider use is recorded ONLY after the customer insert is committed.
    try:
        await _record_email_provider_use_after_commit(email_bundle)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("failed to record provider use after commit: %s", exc)
    # 仅 giffgaff 注册需要英国身份；CTExcel 订单不生成无关资料。
    identity = (
        await regenerate_identity(customer_id) or {}
        if product_type == "giffgaff"
        else {}
    )

    return {
        "customer_id": customer_id,
        "product_type": product_type,
        "message": message,
        "email": email_bundle.get("email", ""),
        "email_provider_id": email_bundle.get("email_provider_id"),
        "email_provider_domain": email_bundle.get("email_provider_domain"),
        "sim_activation_code": sim_activation_code,
        "initial_password": initial_password,
        "first_name": identity.get("first_name"),
        "last_name": identity.get("last_name"),
        "address": identity.get("address"),
        "city": identity.get("city"),
        "postcode": identity.get("postcode"),
    }


@app.patch("/api/customers/{customer_id}", status_code=200)
async def edit_customer(customer_id: int, data: CustomerUpdate):
    c = await get_customer(customer_id)
    if not c:
        raise HTTPException(status_code=404, detail="客户不存在")
    phone_number = normalize_optional_text(data.phone_number)
    if phone_number:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            db.row_factory = aiosqlite.Row
            existing = await fetch_one(
                db,
                "SELECT id FROM customers WHERE phone_number = ? AND id != ?",
                (phone_number, customer_id),
            )
            if existing:
                raise HTTPException(status_code=409, detail="该手机号已录入")
    try:
        await update_customer(customer_id, data)
    except aiosqlite.IntegrityError:
        raise HTTPException(status_code=409, detail="该手机号已录入") from None
    return {"ok": True}


@app.patch("/api/customers/{customer_id}/activation-status", status_code=200)
async def update_customer_activation_status(customer_id: int, data: ActivationStatusUpdate):
    c = await get_customer(customer_id)
    if not c:
        raise HTTPException(status_code=404, detail="客户不存在")
    if _normalize_product_type(c.get("product_type")) == "ctexcel":
        raise HTTPException(status_code=400, detail="CTExcel 客户不使用 SIM 激活流程")
    await _apply_activation_status(customer_id, data.status, data.error)
    message = data.message or f"后台手动标记激活状态：{data.status}"
    await _insert_activation_log(customer_id, "info", data.step or "admin", message)
    return {"ok": True}


@app.post("/api/customers/{customer_id}/reset", status_code=200)
async def reset_customer(customer_id: int, data: ResetCustomerRequest):
    """Reset a customer so they can be re-issued/re-activated.

    Lets operators recover from:
    * accidental "已完成" (which locks the SIM into '已使用')
    * 需要重新人工处理的旧客户
    * 长时间停留在「激活中」的客户

    All three sub-flags default to True (full reset) but can be set
    independently to detach just the SIM, just the email, or just the
    activation state.
    """
    import logging
    log = logging.getLogger(__name__)
    c = await get_customer(customer_id)
    if not c:
        raise HTTPException(status_code=404, detail="客户不存在")
    sim_code_id = c.get("sim_code_id")
    phone_number = c.get("phone_number")
    detached: list[str] = []
    async with aiosqlite.connect(DATABASE_PATH) as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            if data.detach_sim_code and sim_code_id:
                # Bring the SIM back into the pool so it can be re-allocated.
                await db.execute(
                    """UPDATE sim_codes
                       SET status = '未分配', customer_id = NULL,
                           updated_at = datetime('now')
                       WHERE id = ?""",
                    (sim_code_id,),
                )
                await db.execute(
                    "UPDATE customers SET sim_code_id = NULL, sim_activation_code = NULL, initial_password = NULL WHERE id = ?",
                    (customer_id,),
                )
                detached.append("sim_code")
            if data.detach_email:
                await db.execute(
                    """UPDATE customers
                       SET email = '', moemail_id = NULL, moemail_address = NULL,
                           share_link = NULL, email_provider_id = NULL,
                           email_account_id = NULL, email_provider_domain = NULL,
                           is_moemail_auto = 0
                       WHERE id = ?""",
                    (customer_id,),
                )
                # Detach phone_number when email is detached so the row
                # looks like a fresh import (otherwise re-import will clash
                # via the UNIQUE constraint on phone_number).
                if phone_number:
                    await db.execute("UPDATE customers SET phone_number = NULL WHERE id = ?", (customer_id,))
                    detached.append("email+phone")
                else:
                    detached.append("email")
            if data.reset_activation:
                await db.execute(
                    """UPDATE customers
                       SET activation_status = '未开始', activation_error = NULL,
                           activated_at = NULL, automation_lock_owner = NULL,
                           automation_locked_at = NULL
                       WHERE id = ?""",
                    (customer_id,),
                )
                detached.append("activation")
            await db.execute(
                """INSERT INTO activation_logs (customer_id, level, step, message)
                   VALUES (?, 'info', 'reset', ?)""",
                (customer_id, f"已重置客户：{', '.join(detached) or '无'}"),
            )
            await db.commit()
        except Exception as exc:
            await db.rollback()
            log.exception("reset_customer %s failed: %s", customer_id, exc)
            raise HTTPException(status_code=500, detail=f"重置失败：{exc}") from exc
    return {"ok": True, "detached": detached}


@app.put("/api/customers/{customer_id}/esim-code")
async def save_customer_esim_code(customer_id: int, data: EsimCodeUpdate):
    c = await get_customer(customer_id)
    if not c:
        raise HTTPException(status_code=404, detail="客户不存在")
    if _normalize_product_type(c.get("product_type")) == "ctexcel":
        raise HTTPException(status_code=400, detail="CTExcel 客户不使用 eSIM 激活码二维码")
    raw = (data.code or "").strip()
    if raw and not parse_esim_raw(raw):
        raise HTTPException(status_code=400, detail="eSIM 激活码格式无效，需为 1$SM-DP+$激活码")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE customers SET esim_raw_code = ? WHERE id = ?",
            (raw or None, customer_id),
        )
        await db.commit()
    return {"ok": True, "esim_raw_code": raw or None}


@app.get("/api/customers/{customer_id}/esim-qr.png")
async def get_customer_esim_qr(customer_id: int):
    c = await get_customer(customer_id)
    if not c:
        raise HTTPException(status_code=404, detail="客户不存在")
    if _normalize_product_type(c.get("product_type")) == "ctexcel":
        raise HTTPException(status_code=400, detail="CTExcel 客户不使用 eSIM 激活码二维码")
    raw = (c.get("esim_raw_code") or "").strip()
    if not raw:
        raise HTTPException(status_code=404, detail="该客户尚未保存 eSIM 激活码")
    parsed = parse_esim_raw(raw)
    if not parsed:
        raise HTTPException(status_code=400, detail="保存的 eSIM 激活码格式无效")
    smdp, code = parsed
    lpa = build_lpa_string(smdp, code)
    png_bytes = generate_esim_qr_png(lpa)
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "no-store", "X-LPA-String": lpa},
    )


@app.delete("/api/customers/{customer_id}", status_code=200)
async def remove_customer(customer_id: int):
    c = await get_customer(customer_id)
    if not c:
        raise HTTPException(status_code=404, detail="客户不存在")
    sim_code_id = c.get("sim_code_id")
    if sim_code_id:
        status = _normalize_activation_status(c.get("activation_status"))
        sim_status = "已使用" if status in {"等待转 eSIM", "已完成"} else "未分配"
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                """UPDATE sim_codes
                   SET status = ?, customer_id = NULL, updated_at = datetime('now')
                   WHERE id = ?""",
                (sim_status, sim_code_id),
            )
            await db.execute("DELETE FROM activation_logs WHERE customer_id = ?", (customer_id,))
            await db.commit()
    await delete_customer(customer_id)
    return {"ok": True}


@app.post("/api/customers/{customer_id}/public-link/regenerate", status_code=200)
async def regenerate_public_link_route(customer_id: int):
    """旋转 public_token、public_version +1。
    旧 token 在 DB 中立即失效（Worker 再回调 /api/public/{old}/version 会 404）。
    客户端用新 public_token 拼装新 QR。"""
    result = await regenerate_public_link(customer_id)
    if not result:
        raise HTTPException(status_code=404, detail="客户不存在")
    return result


@app.post("/api/customers/{customer_id}/public-link/ensure", status_code=200)
async def ensure_public_link_route(customer_id: int):
    """只为尚无 token 的旧客户按需生成公开链接；已有链接保持不变。"""
    result = await ensure_public_link(customer_id)
    if not result:
        raise HTTPException(status_code=404, detail="客户不存在")
    return result


@app.post("/api/customers/{customer_id}/identity/regenerate", status_code=200)
async def regenerate_identity_route(customer_id: int):
    """重新随机 first_name / last_name / address / city / postcode。
    覆盖已存在的值，返回新的身份信息。"""
    customer = await get_customer(customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    if _normalize_product_type(customer.get("product_type")) == "ctexcel":
        raise HTTPException(status_code=400, detail="CTExcel 客户不使用随机身份和英国地址")
    result = await regenerate_identity(customer_id)
    if not result:
        raise HTTPException(status_code=404, detail="客户不存在")
    return result


@app.post("/api/customers/{customer_id}/moemail")
async def create_customer_moemail(customer_id: int, data: MoEmailCreateRequest):
    c = await get_customer(customer_id)
    if not c:
        raise HTTPException(status_code=404, detail="客户不存在")
    try:
        email_bundle = await _generate_email_account()
        # Bridge to legacy fields so update_customer_moemail (which writes old columns) works
        await update_customer_moemail(
            customer_id,
            email_bundle["email_account_id"],
            email_bundle["email"],
            email_bundle.get("share_link", ""),
            True,
            email_provider_id=email_bundle.get("email_provider_id"),
            email_provider_domain=email_bundle.get("email_provider_domain"),
        )
        return {
            "ok": True,
            "email": email_bundle["email"],
            "moemail_id": email_bundle["email_account_id"],
            "email_provider_id": email_bundle["email_provider_id"],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"为客户生成邮箱失败：{exc}") from exc


# ── Inbox provider resolution ──


async def _resolve_inbox_provider(customer_row: dict) -> tuple[str, "MoEmailClient | CloudMailProvider"]:
    """Pick the right provider client for fetching a customer's inbox messages.

    Strategy:
    1. If customer has email_provider_id pointing to a pool entry → use that provider.
    2. Otherwise (legacy customer) → find first MoEmail-type provider in pool.
    3. If no MoEmail in pool → construct ad-hoc MoEmailClient from global settings
       (preserves pre-pool behavior for users who never set up pool entries).

    Returns (provider_account_id_on_provider, provider_client_instance).
    Raises HTTPException(400) if no usable provider exists.
    """
    provider_id = customer_row.get("email_provider_id")
    if provider_id:
        pid, provider = get_provider(DATABASE_PATH, int(provider_id))
        if not provider:
            raise HTTPException(
                status_code=400,
                detail=f"客户关联的邮箱 provider(id={provider_id}) 不存在，请联系管理员",
            )
        account_id = customer_row.get("email_account_id") or customer_row.get("moemail_id")
        if not account_id:
            raise HTTPException(status_code=400, detail="客户无邮箱账号信息")
        return str(account_id), provider

    # Legacy fallback: legacy customers were created when only MoEmail existed.
    rows = list_providers(DATABASE_PATH)
    moemail_rows = [r for r in rows if r["provider_type"] == "moemail"]
    if moemail_rows:
        r = moemail_rows[0]
        pid, provider = get_provider(DATABASE_PATH, r["id"])
        legacy_id = customer_row.get("moemail_id")
        if not legacy_id:
            raise HTTPException(status_code=400, detail="该客户没有 MoEmail 邮箱")
        return str(legacy_id), provider

    # Final fallback: legacy global MoEmail settings (pre-pool users).
    # These keys may still be set on older deployments; we still honor them
    # for legacy customers so historical inboxes keep working.
    moemail_url = _normalize_base_url(await _get_setting("moemail_url"))
    moemail_key = await _get_setting("moemail_api_key")
    if not moemail_url or not moemail_key:
        raise HTTPException(
            status_code=400,
            detail=(
                "尚未配置邮箱服务商。请在「邮箱服务商」标签里添加一个 MoEmail 或 Cloud-Mail provider，"
                "并把客户改用 email_provider_id 关联；或者重新录入客户让后台分配新 provider。"
            ),
        )
    from email_providers._moemail_client import MoEmailClient
    legacy_id = customer_row.get("moemail_id")
    if not legacy_id:
        raise HTTPException(status_code=400, detail="该客户没有 MoEmail 邮箱")
    return str(legacy_id), MoEmailClient(moemail_url, moemail_key)


@app.get(
    "/api/customers/{customer_id}/inbox",
    response_model=InboxMessageListOut,
)
async def get_customer_inbox(customer_id: int, response: Response):
    """Return every message summary currently exposed by the assigned provider.

    This endpoint intentionally loads summaries only. The body is fetched on
    demand by ``/inbox-message`` so a large mailbox does not trigger one origin
    request per message.
    """
    c = await get_customer(customer_id)
    if not c:
        raise HTTPException(status_code=404, detail="客户不存在")
    response.headers["Cache-Control"] = "no-store"
    account_id, client = await _resolve_inbox_provider(c)
    email_address = c.get("moemail_address") or c.get("email") or ""
    try:
        mailbox = await asyncio.to_thread(client.get_email_messages, account_id)
        messages = _message_list(mailbox)
        messages.sort(key=_message_received_at, reverse=True)
        summaries = [
            InboxMessageSummaryOut(
                id=_message_id(message),
                subject=_first_text(message, "subject") or "（无主题）",
                from_address=_message_address(
                    message, "fromAddress", "from_address", "from", "sender"
                ),
                sent_at=_message_sent_at(message),
                received_at=_message_received_at(message),
            )
            for message in messages
            if _message_id(message)
        ]
        return InboxMessageListOut(
            email=email_address,
            count=len(summaries),
            messages=summaries,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"读取邮箱列表失败：{exc}") from exc


@app.get(
    "/api/customers/{customer_id}/inbox-message",
    response_model=InboxMessageDetailOut,
)
async def get_customer_inbox_message(
    customer_id: int,
    response: Response,
    message_id: str = Query(min_length=1, max_length=512),
):
    """Fetch one full message body directly from the assigned provider."""
    c = await get_customer(customer_id)
    if not c:
        raise HTTPException(status_code=404, detail="客户不存在")
    response.headers["Cache-Control"] = "no-store"
    account_id, client = await _resolve_inbox_provider(c)
    try:
        mailbox = await asyncio.to_thread(
            client.get_email_messages,
            account_id,
        )
        summaries = _message_list(mailbox)
        summary = next(
            (item for item in summaries if _message_id(item) == message_id),
            {},
        )
        detail = {}
        try:
            detail_payload = await asyncio.to_thread(
                client.get_message,
                account_id,
                message_id,
            )
            detail = _message_detail_payload(detail_payload)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404 or not summary:
                raise
        except Exception:
            if not (
                _message_body_text(summary)
                or _message_html_body(summary)
            ):
                raise
        message = dict(summary)
        for key, value in detail.items():
            if value not in (None, "", [], {}):
                message[key] = value
        if not message or (not summary and not detail):
            raise HTTPException(status_code=404, detail="邮件不存在或已失效")
        return InboxMessageDetailOut(
            id=_message_id(message) or message_id,
            subject=_first_text(message, "subject") or "（无主题）",
            from_address=_message_address(
                message, "fromAddress", "from_address", "from", "sender"
            ),
            to_address=_message_address(
                message, "toAddress", "to_address", "to", "recipient", "recipients"
            ),
            sent_at=_message_sent_at(message),
            received_at=_message_received_at(message),
            body=_message_body_text(message),
            html_body=_message_html_body(message),
        )
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(status_code=404, detail="邮件不存在或已失效") from exc
        raise HTTPException(status_code=502, detail=f"读取邮件正文失败：{exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"读取邮件正文失败：{exc}") from exc


@app.get("/api/customers/{customer_id}/verification-code", response_model=VerificationCodeOut)
async def get_customer_verification_code(customer_id: int):
    c = await get_customer(customer_id)
    if not c:
        raise HTTPException(status_code=404, detail="客户不存在")
    moemail_id, client = await _resolve_inbox_provider(c)
    email_address = c.get("moemail_address") or c.get("email") or ""
    try:
        mailbox = client.get_email_messages(moemail_id)
        messages = _message_list(mailbox)
        messages.sort(key=_message_received_at, reverse=True)
        checked_count = 0
        detail_miss_count = 0
        latest_meta = {}

        for summary in messages[:10]:
            message_id = _message_id(summary)
            detail = {}
            if message_id:
                try:
                    detail = _message_detail_payload(client.get_message(moemail_id, message_id))
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        raise
                    detail_miss_count += 1
            message = {**summary, **detail}
            checked_count += 1
            if not latest_meta:
                latest_meta = message
            code = _extract_verification_code(message)
            if code:
                return VerificationCodeOut(
                    found=True,
                    code=code,
                    email=email_address,
                    message_id=_message_id(message) or message_id or None,
                    subject=_first_text(message, "subject") or None,
                    from_address=_first_text(message, "fromAddress", "from_address", "from") or None,
                    received_at=_message_received_at(message) or None,
                    checked_count=checked_count,
                    detail="已提取最新验证码",
                )

        return VerificationCodeOut(
            found=False,
            email=email_address,
            message_id=_message_id(latest_meta) or None,
            subject=_first_text(latest_meta, "subject") or None,
            from_address=_first_text(latest_meta, "fromAddress", "from_address", "from") or None,
            received_at=_message_received_at(latest_meta) or None,
            checked_count=checked_count,
            detail=(
                f"没有找到可提取的 6 位验证码；{detail_miss_count} 封邮件详情已不存在或接口未返回"
                if detail_miss_count
                else "没有找到可提取的 6 位验证码"
            ),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"邮箱接码失败：{e}") from e


async def _list_pending_ctexcel_client_customers(limit: int = 1000) -> list[dict]:
    """列出尚未同步手机号的 CTExcel 客户，供桌面客户端恢复流程。"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            """SELECT id, email, phone_number, ctexcel_order_number,
                      ctexcel_registration_confirmed_at,
                      ctexcel_payment_succeeded_at,
                      ctexcel_last_checked_at, created_at
               FROM customers
               WHERE product_type = 'ctexcel'
                 AND NULLIF(TRIM(phone_number), '') IS NULL
                 AND NULLIF(
                       TRIM(ctexcel_registration_confirmed_at), ''
                     ) IS NULL
                 AND NULLIF(
                       TRIM(ctexcel_payment_succeeded_at), ''
                     ) IS NULL
               ORDER BY id ASC
               LIMIT ?""",
            (min(max(1, int(limit)), 1000),),
        )
    return [
        {
            "customer_id": int(row["id"]),
            "email": row["email"],
            "phone_number": row["phone_number"],
            "order_number": row["ctexcel_order_number"],
            "registration_confirmed_at": row[
                "ctexcel_registration_confirmed_at"
            ],
            "payment_succeeded_at": row[
                "ctexcel_payment_succeeded_at"
            ],
            "last_checked_at": row["ctexcel_last_checked_at"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


async def _get_ctexcel_client_customer_by_request_key(
    request_key: str,
) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await fetch_one(
            db,
            """SELECT id, email, phone_number, ctexcel_order_number,
                      ctexcel_registration_confirmed_at,
                      ctexcel_payment_succeeded_at,
                      ctexcel_last_checked_at, created_at
               FROM customers
               WHERE product_type = 'ctexcel'
                 AND ctexcel_client_request_key = ?""",
            (request_key,),
        )
    if not row:
        return None
    return {
        "customer_id": int(row["id"]),
        "email": row["email"],
        "phone_number": row["phone_number"],
        "order_number": row["ctexcel_order_number"],
        "registration_confirmed_at": row[
            "ctexcel_registration_confirmed_at"
        ],
        "payment_succeeded_at": row[
            "ctexcel_payment_succeeded_at"
        ],
        "last_checked_at": row["ctexcel_last_checked_at"],
        "created_at": row["created_at"],
    }


async def _save_ctexcel_client_request_key(
    customer_id: int,
    request_key: Optional[str],
) -> None:
    if not request_key:
        return
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """UPDATE customers
               SET ctexcel_client_request_key = ?
               WHERE id = ? AND product_type = 'ctexcel'""",
            (request_key, customer_id),
        )
        await db.commit()


@app.get("/api/ctexcel-client/status")
async def get_ctexcel_client_status(request: Request, response: Response):
    """桌面客户端连通性检查；只返回 CTExcel 范围内的非敏感状态。"""
    _require_ctexcel_client(request)
    response.headers["Cache-Control"] = "no-store"
    async with aiosqlite.connect(DATABASE_PATH) as db:
        rows = await db.execute_fetchall(
            """SELECT COUNT(*) AS total,
                      SUM(
                        CASE WHEN NULLIF(TRIM(phone_number), '') IS NULL
                                  AND NULLIF(
                                        TRIM(ctexcel_registration_confirmed_at), ''
                                      ) IS NULL
                                  AND NULLIF(
                                        TRIM(ctexcel_payment_succeeded_at), ''
                                      ) IS NULL
                             THEN 1 ELSE 0 END
                      ) AS pending
               FROM customers
               WHERE product_type = 'ctexcel'"""
        )
    total = int(rows[0][0] or 0) if rows else 0
    pending = int(rows[0][1] or 0) if rows else 0
    return {
        "ok": True,
        "api_version": 8,
        "ctexcel_customer_count": total,
        "pending_customer_count": pending,
    }


@app.get("/api/ctexcel-client/customers/pending")
async def get_ctexcel_client_pending_customers(
    request: Request,
    response: Response,
    limit: int = Query(1000, ge=1, le=1000),
):
    """返回无手机号客户列表；不暴露普通客户管理接口。"""
    _require_ctexcel_client(request)
    response.headers["Cache-Control"] = "no-store"
    customers = await _list_pending_ctexcel_client_customers(limit)
    return {
        "count": len(customers),
        "customers": customers,
    }


@app.post("/api/ctexcel-client/customers", status_code=201)
async def create_ctexcel_client_customer(
    data: CTExcelClientCustomerCreate,
    request: Request,
):
    """优先复用中断客户，否则创建 CTExcel 客户和专属托管邮箱。"""
    _require_ctexcel_client(request)
    request_key = normalize_optional_text(data.request_key)
    if request_key:
        existing = await _get_ctexcel_client_customer_by_request_key(
            request_key
        )
        if existing:
            return {
                **existing,
                "product_type": "ctexcel",
                "sim_activation_code": None,
                "reused": True,
                "idempotent_replay": True,
            }
    pending = await _list_pending_ctexcel_client_customers()
    unfinished_pending = [
        customer
        for customer in pending
        if not normalize_optional_text(
            customer.get("registration_confirmed_at")
        )
        and not normalize_optional_text(
            customer.get("payment_succeeded_at")
        )
    ]
    resumable = None
    if data.resume_customer_id is not None:
        resumable = next(
            (
                customer
                for customer in unfinished_pending
                if int(customer["customer_id"])
                == int(data.resume_customer_id)
            ),
            None,
        )
        if resumable is None:
            raise HTTPException(
                status_code=409,
                detail="指定的待补全 CTExcel 客户已完成或已被处理",
            )
    elif unfinished_pending:
        resumable = unfinished_pending[0]
    if data.reuse_pending and resumable:
        await _save_ctexcel_client_request_key(
            int(resumable["customer_id"]),
            request_key,
        )
        return {
            **resumable,
            "product_type": "ctexcel",
            "sim_activation_code": None,
            "reused": True,
            "pending_customer_count": len(pending),
        }
    try:
        created = await add_customer(
            CustomerCreate(
                product_type="ctexcel",
                email="",
                shipping_address=None,
                phone_status="激活",
                activation_date=datetime.date.today(),
                use_sim_code=False,
            )
        )
        await _save_ctexcel_client_request_key(
            int(created["customer_id"]),
            request_key,
        )
        return {
            **created,
            "reused": False,
            "pending_customer_count": len(pending) + 1,
        }
    except HTTPException as exc:
        if exc.status_code < 500:
            raise
        logging.getLogger(__name__).warning(
            "CTExcel client customer creation failed: %s",
            exc.detail,
        )
        raise HTTPException(
            status_code=502,
            detail="服务器创建 CTExcel 客户或专属邮箱失败",
        ) from exc


@app.post(
    "/api/ctexcel-client/customers/{customer_id}/payment-checkpoint",
)
async def save_ctexcel_client_payment_checkpoint(
    customer_id: int,
    data: CTExcelPaymentCheckpointRequest,
    request: Request,
    response: Response,
):
    """保存桌面客户端支付页确认过的订单号和英镑金额。"""
    _require_ctexcel_client(request)
    response.headers["Cache-Control"] = "no-store"
    customer = await get_customer(customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    if _normalize_product_type(customer.get("product_type")) != "ctexcel":
        raise HTTPException(status_code=400, detail="该客户不是 CTExcel 模式")

    raw_amount = str(data.transaction_amount or "").strip()
    try:
        amount = Decimal(raw_amount).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="付款金额格式错误")
    if (
        not amount.is_finite()
        or amount <= 0
        or amount > Decimal("10000")
    ):
        raise HTTPException(status_code=400, detail="付款金额超出允许范围")

    order_number = normalize_optional_text(data.order_number)
    if order_number:
        order_number = order_number.upper()
        if not re.fullmatch(r"[A-Z0-9-]{8,80}", order_number):
            raise HTTPException(status_code=400, detail="CTExcel 订单号格式错误")
    phone_number = normalize_optional_text(data.phone_number)
    if phone_number:
        phone_number = re.sub(r"[\s()-]+", "", phone_number)
        if not re.fullmatch(r"(?:\+?44|0)7\d{9}", phone_number):
            raise HTTPException(
                status_code=400,
                detail="CTExcel 手机号码格式错误",
            )
    normalized_amount = f"{amount:.2f}"
    payment_succeeded_at = _utc_now() if data.payment_succeeded else None
    saved = await save_ctexcel_payment_checkpoint(
        customer_id,
        order_number=order_number,
        transaction_amount=normalized_amount,
        phone_number=phone_number,
        payment_succeeded_at=payment_succeeded_at,
    )
    if not saved:
        raise HTTPException(status_code=404, detail="CTExcel 客户不存在")
    return {
        "ok": True,
        "customer_id": customer_id,
        "order_number": order_number or customer.get("ctexcel_order_number"),
        "transaction_amount": normalized_amount,
        "phone_number": phone_number or customer.get("phone_number"),
        "payment_succeeded": bool(data.payment_succeeded),
        "payment_succeeded_at": (
            payment_succeeded_at
            or customer.get("ctexcel_payment_succeeded_at")
        ),
    }


@app.get(
    "/api/ctexcel-client/customers/{customer_id}/verification-code",
    response_model=VerificationCodeOut,
)
async def get_ctexcel_client_verification_code(
    customer_id: int,
    request: Request,
):
    """读取本次 CTExcel 申请专属邮箱中的注册验证码。"""
    _require_ctexcel_client(request)
    customer = await get_customer(customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    if _normalize_product_type(customer.get("product_type")) != "ctexcel":
        raise HTTPException(status_code=400, detail="该客户不是 CTExcel 模式")
    try:
        return await get_customer_verification_code(customer_id)
    except HTTPException as exc:
        if exc.status_code < 500:
            raise
        logging.getLogger(__name__).warning(
            "CTExcel client verification lookup failed for customer %s: %s",
            customer_id,
            exc.detail,
        )
        raise HTTPException(
            status_code=502,
            detail="服务器读取注册验证码失败",
        ) from exc


async def _sync_ctexcel_order_info(
    c: dict,
    limit: int = 50,
) -> CTExcelOrderInfoOut:
    """读取一个 CTExcel 客户邮箱，并持久化订单号、号码和推荐资料。"""
    customer_id = int(c["id"])
    account_id, client = await _resolve_inbox_provider(c)
    limit = min(max(1, limit), 100)
    try:
        mailbox = await asyncio.to_thread(client.get_email_messages, account_id)
        messages = _message_list(mailbox)
        messages.sort(key=_message_received_at, reverse=True)
        found: dict[str, Optional[str]] = {
            "phone_number": None,
            "order_number": None,
            "transaction_amount": None,
            "referral_code": None,
            "referral_link": None,
        }
        matched_message: dict = {}
        confirmation_message: dict = {}
        registration_confirmed_at = normalize_optional_text(
            c.get("ctexcel_registration_confirmed_at")
        )
        checked_count = 0
        detail_miss_count = 0

        for summary in messages[:limit]:
            message_id = _message_id(summary)
            detail = {}
            summary_info = _extract_ctexcel_order_info(summary)
            confirmation_detected = (
                _is_ctexcel_registration_confirmation(summary)
            )
            if (
                message_id
                and not confirmation_detected
                and not (
                    summary_info.get("phone_number")
                    and summary_info.get("order_number")
                )
            ):
                try:
                    detail = _message_detail_payload(
                        await asyncio.to_thread(
                            client.get_message,
                            account_id,
                            message_id,
                        )
                    )
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        raise
                    detail_miss_count += 1
            message = {**summary, **detail}
            checked_count += 1
            confirmation_detected = (
                confirmation_detected
                or _is_ctexcel_registration_confirmation(message)
            )
            if confirmation_detected:
                if not confirmation_message:
                    confirmation_message = message
                if not registration_confirmed_at:
                    registration_confirmed_at = (
                        _message_received_at(message)
                        or _message_sent_at(message)
                        or _utc_now()
                    )
            parsed = _extract_ctexcel_order_info(message)
            if (
                (any(parsed.values()) or confirmation_detected)
                and not matched_message
            ):
                matched_message = message
            for key in found:
                if not found[key] and parsed.get(key):
                    found[key] = parsed[key]
            if found["phone_number"] and found["order_number"] and all(
                found[key] for key in ("transaction_amount", "referral_code", "referral_link")
            ):
                break

        checked_at = _utc_now()
        persisted = await save_ctexcel_order_info(
            customer_id,
            phone_number=found["phone_number"],
            order_number=found["order_number"],
            transaction_amount=found["transaction_amount"],
            referral_code=found["referral_code"],
            referral_link=found["referral_link"],
            registration_confirmed_at=registration_confirmed_at,
            checked_at=checked_at,
        )
        if not persisted:
            raise RuntimeError("CTExcel 订单资料未写入客户记录")

        output = {
            "phone_number": found["phone_number"] or c.get("phone_number"),
            "order_number": found["order_number"] or c.get("ctexcel_order_number"),
            "transaction_amount": found["transaction_amount"] or c.get("ctexcel_transaction_amount"),
            "referral_code": found["referral_code"] or c.get("ctexcel_referral_code"),
            "referral_link": found["referral_link"] or c.get("ctexcel_referral_link"),
        }
        registration_confirmed = bool(registration_confirmed_at)
        has_core_info = bool(
            output["phone_number"]
            or output["order_number"]
            or registration_confirmed
        )
        detail_text = (
            "已从 CTExcel 订单邮件同步手机号码和订单资料"
            if found["phone_number"] and found["order_number"]
            else (
                "已确认该邮箱完成 CTExcel 注册；等待订单号和手机号同步"
                if registration_confirmed
                and not (output["phone_number"] or output["order_number"])
                else (
                    "已读取邮件，但只提取到部分 CTExcel 订单资料"
                    if any(found.values())
                    else "没有找到 CTExcel 订单确认邮件或号码资料"
                )
            )
        )
        if detail_miss_count:
            detail_text += f"；{detail_miss_count} 封邮件详情已失效"
        return CTExcelOrderInfoOut(
            found=has_core_info,
            registration_confirmed=registration_confirmed,
            registration_confirmed_at=registration_confirmed_at,
            **output,
            message_id=_message_id(matched_message) or None,
            subject=_first_text(matched_message, "subject") or None,
            from_address=_message_address(
                matched_message, "fromAddress", "from_address", "from", "sender"
            ) or None,
            received_at=(
                _message_received_at(matched_message)
                or _message_sent_at(matched_message)
                or None
            ),
            checked_count=checked_count,
            detail=detail_text,
        )
    except aiosqlite.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="邮件中的 CTExcel 手机号码已关联其他客户") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"CTExcel 订单邮件检查失败：{exc}") from exc


@app.post(
    "/api/ctexcel-client/customers/{customer_id}/order-info",
    response_model=CTExcelOrderInfoOut,
)
async def sync_ctexcel_client_order_info(
    customer_id: int,
    request: Request,
):
    """供桌面客户端在恢复流程和支付完成后立即同步订单邮件。"""
    _require_ctexcel_client(request)
    customer = await get_customer(customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    if _normalize_product_type(customer.get("product_type")) != "ctexcel":
        raise HTTPException(status_code=400, detail="该客户不是 CTExcel 模式")
    try:
        return await _sync_ctexcel_order_info(customer, limit=50)
    except HTTPException as exc:
        if exc.status_code < 500:
            raise
        logging.getLogger(__name__).warning(
            "CTExcel client order sync failed for customer %s: %s",
            customer_id,
            exc.detail,
        )
        raise HTTPException(
            status_code=502,
            detail="服务器读取 CTExcel 订单邮件失败",
        ) from exc


@app.get(
    "/api/customers/{customer_id}/ctexcel-order-info",
    response_model=CTExcelOrderInfoOut,
)
async def get_customer_ctexcel_order_info(customer_id: int, limit: int = 50):
    """扫描 CTExcel 订单邮件，并把手机号、订单号等资料同步到客户记录。"""
    c = await get_customer(customer_id)
    if not c:
        raise HTTPException(status_code=404, detail="客户不存在")
    if _normalize_product_type(c.get("product_type")) != "ctexcel":
        raise HTTPException(status_code=400, detail="该客户不是 CTExcel 模式")
    return await _sync_ctexcel_order_info(c, limit)


@app.get("/api/customers/{customer_id}/payment-info-emails", response_model=PaymentInfoEmailOut)
async def get_customer_payment_info_emails(customer_id: int, limit: int = 50):
    c = await get_customer(customer_id)
    if not c:
        raise HTTPException(status_code=404, detail="客户不存在")
    if _normalize_product_type(c.get("product_type")) == "ctexcel":
        raise HTTPException(status_code=400, detail="CTExcel 模式使用订单资料抓取，不执行支付解绑检查")
    moemail_id, client = await _resolve_inbox_provider(c)
    email_address = c.get("moemail_address") or c.get("email") or ""
    limit = min(max(1, limit), 100)
    try:
        mailbox = client.get_email_messages(moemail_id)
        messages = _message_list(mailbox)
        messages.sort(key=_message_received_at, reverse=True)

        checked_count = 0
        detail_miss_count = 0
        updated_count = 0
        changed_count = 0
        latest_updated = {}
        latest_changed = {}

        for summary in messages[:limit]:
            message_id = _message_id(summary)
            detail = {}
            if message_id:
                try:
                    detail = _message_detail_payload(client.get_message(moemail_id, message_id))
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        raise
                    detail_miss_count += 1
            message = {**summary, **detail}
            checked_count += 1
            kind = _payment_info_email_kind(message)
            if kind == "updated":
                updated_count += 1
                if not latest_updated:
                    latest_updated = message
            elif kind == "changed":
                changed_count += 1
                if not latest_changed:
                    latest_changed = message

        detail = (
            f"检测到支付信息更新邮件 {updated_count} 封，取消/变更邮件 {changed_count} 封"
            if updated_count or changed_count
            else "没有检测到支付信息变更邮件"
        )
        if detail_miss_count:
            detail += f"；{detail_miss_count} 封邮件详情已不存在或接口未返回"
        # 持久化结果：只有数据库确认写入后才返回查询成功，避免详情显示
        # “已解绑”而首页仍停留在旧状态。
        now_iso = datetime.datetime.utcnow().isoformat() + "Z"
        latest_changed_at = (
            _message_received_at(latest_changed)
            or _message_sent_at(latest_changed)
            or (now_iso if changed_count else None)
        )
        latest_updated_at = (
            _message_received_at(latest_updated)
            or _message_sent_at(latest_updated)
            or (now_iso if updated_count else None)
        )
        persisted = await save_payment_check_result(
            customer_id,
            changed_at=latest_changed_at,
            updated_at=latest_updated_at,
            checked_at=now_iso,
        )
        if not persisted:
            raise RuntimeError("支付查询结果未写入客户记录")
        return PaymentInfoEmailOut(
            found=changed_count > 0,
            updated_found=updated_count > 0,
            changed_found=changed_count > 0,
            updated_count=updated_count,
            changed_count=changed_count,
            email=email_address,
            checked_count=checked_count,
            latest_updated_message_id=_message_id(latest_updated) or None,
            latest_updated_subject=_first_text(latest_updated, "subject") or None,
            latest_updated_received_at=latest_updated_at,
            latest_changed_message_id=_message_id(latest_changed) or None,
            latest_changed_subject=_first_text(latest_changed, "subject") or None,
            latest_changed_received_at=latest_changed_at,
            detail=detail,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"支付信息邮件检查失败：{e}") from e


# ── MoEmail 域名列表 ──

@app.get("/api/moemail/domains", response_model=DomainInfo)
async def list_moemail_domains():
    moemail_url = _normalize_base_url(await _get_setting("moemail_url"))
    moemail_key = await _get_setting("moemail_api_key")
    if not moemail_url or not moemail_key:
        raise HTTPException(status_code=400, detail="MoEmail 未配置")
    from moemail import MoEmailClient
    client = MoEmailClient(moemail_url, moemail_key)
    try:
        return DomainInfo(domains=client.get_domains())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取域名失败：{e}")


# ── SIM 激活码库 ──

def _parse_sim_codes(data: SimCodeImport) -> list[str]:
    values = []
    if data.codes:
        values.extend(data.codes)
    if data.text:
        values.extend(re.split(r"[\s,;，；]+", data.text))
    seen = set()
    codes = []
    for value in values:
        code = _normalize_sim_code(value)
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def _sim_code_out(row) -> SimCodeOut:
    return SimCodeOut(
        id=row["id"],
        code=row["code"],
        status=_normalize_sim_code_status(row["status"]),
        customer_id=row["customer_id"],
        notes=row["notes"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )



async def _detach_sim_code_from_customer(
    db: aiosqlite.Connection,
    customer_id: int,
    sim_code: str,
    reason: str,
) -> None:
    await db.execute(
        """UPDATE customers
           SET sim_code_id = NULL,
               sim_activation_code = NULL,
               initial_password = NULL,
               activation_status = '未开始',
               activation_error = NULL,
               activated_at = NULL,
               automation_lock_owner = NULL,
               automation_locked_at = NULL
           WHERE id = ?""",
        (customer_id,),
    )
    await db.execute(
        """INSERT INTO activation_logs (customer_id, level, step, message)
           VALUES (?, 'info', 'sim-code', ?)""",
        (customer_id, f"已取消使用 SIM 激活码 {sim_code}（{reason}）"),
    )


@app.get("/api/sim-codes", response_model=list[SimCodeOut])
async def list_sim_codes():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall("SELECT * FROM sim_codes ORDER BY id DESC LIMIT 1000")
    return [_sim_code_out(row) for row in rows]


@app.post("/api/sim-codes/import", status_code=201)
async def import_sim_codes(data: SimCodeImport):
    codes = _parse_sim_codes(data)
    if not codes:
        raise HTTPException(status_code=400, detail="请粘贴或填写 SIM 激活码")
    imported = 0
    async with aiosqlite.connect(DATABASE_PATH) as db:
        for code in codes:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO sim_codes (code, status) VALUES (?, '未分配')",
                (code,),
            )
            imported += cursor.rowcount
        await db.commit()
    return {
        "ok": True,
        "imported": imported,
        "duplicates": len(codes) - imported,
        "total": len(codes),
    }


@app.patch("/api/sim-codes/{sim_code_id}", response_model=SimCodeOut)
async def update_sim_code(sim_code_id: int, data: SimCodeUpdate):
    status = _normalize_sim_code_status(data.status)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("BEGIN IMMEDIATE")
            row = await fetch_one(db, "SELECT * FROM sim_codes WHERE id = ?", (sim_code_id,))
            if not row:
                raise HTTPException(status_code=404, detail="激活码不存在")
            customer_id = row["customer_id"]
            if customer_id:
                customer = await fetch_one(
                    db,
                    "SELECT id, activation_status FROM customers WHERE id = ?",
                    (customer_id,),
                )
                if not customer:
                    customer_id = None
                elif status in {"未分配", "作废"}:
                    activation_status = _normalize_activation_status(customer["activation_status"])
                    if activation_status not in DETACHABLE_ACTIVATION_STATUSES:
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"该激活码已关联客户 {customer_id}，当前激活状态为「{activation_status}」，"
                                "不能直接改为未分配或作废"
                            ),
                        )
                    await _detach_sim_code_from_customer(
                        db,
                        customer_id,
                        row["code"],
                        "标记为可用" if status == "未分配" else "标记为不用",
                    )
                    customer_id = None

            if status in {"未分配", "作废"}:
                customer_id = None

            await db.execute(
                """UPDATE sim_codes
                   SET status = ?, customer_id = ?, updated_at = datetime('now')
                   WHERE id = ?""",
                (status, customer_id, sim_code_id),
            )
            updated = await fetch_one(db, "SELECT * FROM sim_codes WHERE id = ?", (sim_code_id,))
            await db.commit()
            return _sim_code_out(updated)
        except HTTPException:
            await db.rollback()
            raise
        except Exception:
            await db.rollback()
            raise



@app.delete("/api/sim-codes/{sim_code_id}", status_code=200)
async def delete_sim_code(sim_code_id: int):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("BEGIN IMMEDIATE")
            row = await fetch_one(db, "SELECT * FROM sim_codes WHERE id = ?", (sim_code_id,))
            if not row:
                raise HTTPException(status_code=404, detail="激活码不存在")

            customer_id = row["customer_id"]
            if customer_id:
                customer = await fetch_one(
                    db,
                    "SELECT id, activation_status FROM customers WHERE id = ?",
                    (customer_id,),
                )
                if customer:
                    activation_status = _normalize_activation_status(customer["activation_status"])
                    if activation_status in DETACHABLE_ACTIVATION_STATUSES:
                        await _detach_sim_code_from_customer(db, customer_id, row["code"], "删除激活码")
                    else:
                        await db.execute(
                            "UPDATE customers SET sim_code_id = NULL WHERE id = ?",
                            (customer_id,),
                        )
                        await db.execute(
                            """INSERT INTO activation_logs (customer_id, level, step, message)
                               VALUES (?, 'info', 'sim-code', ?)""",
                            (
                                customer_id,
                                f"已从激活码库删除 SIM 激活码记录 {row['code']}，客户当前激活信息保留",
                            ),
                        )

            await db.execute("DELETE FROM sim_codes WHERE id = ?", (sim_code_id,))
            await db.commit()
            return {"ok": True}
        except HTTPException:
            await db.rollback()
            raise
        except Exception:
            await db.rollback()
            raise


# ── 人工激活状态辅助 ──

def _sim_status_for_activation(status: str) -> str:
    status = _normalize_activation_status(status)
    if status in {"未开始", "已分配激活码"}:
        return "已分配"
    if status in {"激活中", "等待人工支付"}:
        return "激活中"
    if status in {"等待转 eSIM", "已完成"}:
        return "已使用"
    if status == "失败":
        return "失败"
    return "已分配"


async def _insert_activation_log(customer_id: int, level: str, step: Optional[str], message: str):
    if not message:
        return
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO activation_logs (customer_id, level, step, message)
               VALUES (?, ?, ?, ?)""",
            (customer_id, (level or "info").strip() or "info", normalize_optional_text(step), message),
        )
        await db.commit()


async def _apply_activation_status(customer_id: int, status: str, error: Optional[str] = None):
    status = _normalize_activation_status(status)
    sim_status = _sim_status_for_activation(status)
    activated_at_sql = ", activated_at = COALESCE(activated_at, ?)" if status in {"等待转 eSIM", "已完成"} else ""
    params = [status, normalize_optional_text(error)]
    if status in {"等待转 eSIM", "已完成"}:
        params.append(_utc_now())
    params.append(customer_id)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            f"""UPDATE customers
                SET activation_status = ?, activation_error = ?{activated_at_sql},
                    automation_lock_owner = NULL, automation_locked_at = NULL
                WHERE id = ?""",
            params,
        )
        await db.execute(
            """UPDATE sim_codes
               SET status = ?, updated_at = datetime('now')
               WHERE customer_id = ?""",
            (sim_status, customer_id),
        )
        await db.commit()


# ── 标签模板 ──

def _load_label_templates(raw: str):
    if not raw:
        return deepcopy(DEFAULT_LABEL_TEMPLATES)
    try:
        templates = json.loads(raw)
        return _merge_default_label_templates(templates) if isinstance(templates, list) else deepcopy(DEFAULT_LABEL_TEMPLATES)
    except json.JSONDecodeError:
        return deepcopy(DEFAULT_LABEL_TEMPLATES)


def _default_label_template_id(rows: dict, templates: list[dict]) -> Optional[str]:
    printable_ids = [
        str(template.get("id"))
        for template in templates
        if isinstance(template, dict)
        and template.get("id")
        and template.get("id") != "courier-50x40"
    ]
    configured = (rows.get("default_label_template_id") or "").strip()
    return configured if configured in printable_ids else (printable_ids[0] if printable_ids else None)


def _build_provider_config_json(provider_type: str, config: dict) -> str:
    """Validate and serialize provider-specific config to JSON string."""
    if provider_type == "moemail":
        if "url" not in config or "api_key" not in config:
            raise HTTPException(status_code=400, detail="MoEmail 需要 url 和 api_key")
        out = {"url": config["url"].rstrip("/"), "api_key": config["api_key"]}
        # Persist optional expiry_time_ms (0 / None = use the default 7 days).
        if config.get("expiry_time_ms") is not None:
            try:
                out["expiry_time_ms"] = int(config["expiry_time_ms"])
            except (TypeError, ValueError):
                pass
        return json.dumps(out)
    if provider_type == "cloudmail":
        if "url" not in config or "email" not in config or "password" not in config:
            raise HTTPException(status_code=400, detail="Cloud-Mail 需要 url/email/password")
        return json.dumps({
            "url": config["url"].rstrip("/"),
            "email": config["email"],
            "password": config["password"],
            "domain": config.get("domain", ""),
        })
    raise HTTPException(status_code=400, detail=f"未知 provider_type: {provider_type}")


def _hydrate_provider_config_to_dict(row) -> dict:
    """Inverse: row → config dict (without leaking password to UI).

    Defensive against malformed config_json: returns an empty dict instead
    of 500-ing the whole /api/email-providers endpoint.
    """
    raw = row["config_json"] or "{}"
    try:
        cfg = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"_invalid": True}
    typ = row["provider_type"]
    if typ == "moemail":
        return {"url": cfg.get("url", ""), "api_key": cfg.get("api_key", "")}
    if typ == "cloudmail":
        return {
            "url": cfg.get("url", ""),
            "email": cfg.get("email", ""),
            "domain": cfg.get("domain", ""),
            "password_set": bool(cfg.get("password")),
        }
    return {}


def _row_to_email_provider_out(row) -> dict:
    raw_domains = row["domains_json"]
    domains: list[str] = []
    if raw_domains:
        try:
            parsed = json.loads(raw_domains)
            if isinstance(parsed, list):
                domains = [str(d) for d in parsed if d]
        except json.JSONDecodeError:
            domains = []
    default_domain = (row["default_domain"] or "").strip() or None
    disabled = bool(row["disabled"]) if "disabled" in row.keys() else False
    raw_config = row["config_json"] or "{}"
    try:
        cfg_dict = json.loads(raw_config) if raw_config else {}
    except json.JSONDecodeError:
        cfg_dict = {}
    expiry_time_ms = None
    if isinstance(cfg_dict, dict) and "expiry_time_ms" in cfg_dict:
        try:
            expiry_time_ms = int(cfg_dict["expiry_time_ms"])
        except (TypeError, ValueError):
            expiry_time_ms = None
    return {
        "id": row["id"],
        "name": row["name"],
        "provider_type": row["provider_type"],
        "config": _hydrate_provider_config_to_dict(row),
        "domains": domains,
        "default_domain": default_domain,
        "disabled": disabled,
        "expiry_time_ms": expiry_time_ms,
        "last_used_at": row["last_used_at"],
        "last_error": row["last_error"],
        "last_error_at": row["last_error_at"],
        "last_jwt_acquired_at": row["last_jwt_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@app.get("/api/email-providers")
async def list_email_providers():
    rows = list_providers(DATABASE_PATH)
    return [_row_to_email_provider_out(r) for r in rows]


@app.post("/api/email-providers", status_code=201)
async def add_email_provider(data: EmailProviderCreate):
    # Persist top-level `expiry_time_ms` (moemail) inside config_json so the
    # provider row is self-contained.
    config = dict(data.config or {})
    if data.expiry_time_ms is not None and "expiry_time_ms" not in config:
        config["expiry_time_ms"] = data.expiry_time_ms
    config_json = _build_provider_config_json(data.provider_type, config)
    domains_json = json.dumps(data.domains or [], ensure_ascii=False)
    default_domain = (data.default_domain or "").strip() or None
    now = _utc_now()
    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cur = await db.execute(
                """INSERT INTO email_providers
                   (name, provider_type, config_json, domains_json, default_domain,
                    disabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (data.name, data.provider_type, config_json, domains_json, default_domain,
                 1 if data.disabled else 0, now, now),
            )
            provider_id = cur.lastrowid
            await db.commit()
    except aiosqlite.IntegrityError:
        raise HTTPException(status_code=409, detail="名称已存在")
    return {
        "id": provider_id,
        "name": data.name,
        "provider_type": data.provider_type,
        "config": data.config,
        "domains": data.domains or [],
        "default_domain": default_domain,
        "disabled": data.disabled,
        "expiry_time_ms": data.expiry_time_ms,
        "last_used_at": None,
        "last_error": None,
        "last_error_at": None,
        "last_jwt_acquired_at": None,
        "created_at": now,
        "updated_at": now,
    }


@app.get("/api/email-providers/{provider_id}")
async def get_email_provider(provider_id: int):
    rows = list_providers(DATABASE_PATH)
    row = next((r for r in rows if r["id"] == provider_id), None)
    if not row:
        raise HTTPException(status_code=404, detail="Provider 不存在")
    return _row_to_email_provider_out(row)


@app.patch("/api/email-providers/{provider_id}", status_code=200)
async def update_email_provider(provider_id: int, data: EmailProviderUpdate):
    rows = list_providers(DATABASE_PATH)
    row = next((r for r in rows if r["id"] == provider_id), None)
    if not row:
        raise HTTPException(status_code=404, detail="Provider 不存在")
    now = _utc_now()
    fields = []
    values = []
    if data.name is not None:
        fields.append("name = ?"); values.append(data.name)
    if data.config is not None:
        cfg_in = dict(data.config or {})
        if data.expiry_time_ms is not None and "expiry_time_ms" not in cfg_in:
            cfg_in["expiry_time_ms"] = data.expiry_time_ms
        cfg = _build_provider_config_json(row["provider_type"], cfg_in)
        fields.append("config_json = ?"); values.append(cfg)
        # Invalidate cached JWT — new credentials may invalidate it
        fields.append("last_jwt_token = NULL"); fields.append("last_jwt_at = NULL")
    if data.domains is not None:
        fields.append("domains_json = ?"); values.append(json.dumps(data.domains, ensure_ascii=False))
    if data.default_domain is not None:
        fields.append("default_domain = ?"); values.append((data.default_domain or "").strip() or None)
    if data.disabled is not None:
        fields.append("disabled = ?"); values.append(1 if data.disabled else 0)
    if data.expiry_time_ms is not None and "config" not in (data.__dict__ or {}) and data.config is None:
        # No config change requested but expiry_time_ms is. Merge it into existing config_json.
        try:
            current_cfg = json.loads(row["config_json"] or "{}")
        except json.JSONDecodeError:
            current_cfg = {}
        current_cfg["expiry_time_ms"] = data.expiry_time_ms
        fields.append("config_json = ?"); values.append(json.dumps(current_cfg, ensure_ascii=False))
    fields.append("updated_at = ?"); values.append(now)
    values.append(provider_id)
    sql = f"UPDATE email_providers SET {', '.join(fields)} WHERE id = ?"
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(sql, values)
        await db.commit()
    return {"ok": True, "id": provider_id}


@app.post("/api/email-providers/{provider_id}/test")
async def test_email_provider(provider_id: int):
    pid, provider = get_provider(DATABASE_PATH, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider 不存在")
    ok = provider.ping()
    if ok:
        record_provider_use(DATABASE_PATH, provider_id)
        return {"ok": True, "message": "连接成功"}
    record_provider_use(DATABASE_PATH, provider_id, error="ping failed")
    raise HTTPException(status_code=502, detail="Provider 不可达")


@app.delete("/api/email-providers/{provider_id}", status_code=200)
async def delete_email_provider(provider_id: int):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM customers WHERE email_provider_id = ?",
            (provider_id,),
        )
        count = (await cur.fetchone())[0]
        if count > 0:
            raise HTTPException(status_code=409, detail=f"仍有 {count} 个客户使用此 provider")
        await db.execute("DELETE FROM email_providers WHERE id = ?", (provider_id,))
        await db.commit()
    return {"ok": True}


@app.get("/api/label-config", response_model=LabelConfig)
async def get_label_config():
    rows = await get_settings()
    templates = _load_label_templates(rows.get("label_templates", ""))
    return LabelConfig(
        giffgaff_download_url=rows.get("giffgaff_download_url", DEFAULT_GIFFGAFF_DOWNLOAD_URL),
        activation_tutorial_url=rows.get(
            "activation_tutorial_url", DEFAULT_ACTIVATION_TUTORIAL_URL
        ),
        default_template_id=_default_label_template_id(rows, templates),
        templates=templates,
    )


@app.put("/api/label-config")
async def update_label_config(data: LabelConfig):
    rows = await get_settings()
    tutorial_url = data.activation_tutorial_url.strip() or DEFAULT_ACTIVATION_TUTORIAL_URL
    if not tutorial_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="激活教程地址必须以 http:// 或 https:// 开头")
    default_template_id = (data.default_template_id or "").strip()
    printable_ids = {
        str(template.get("id"))
        for template in data.templates
        if isinstance(template, dict)
        and template.get("id")
        and template.get("id") != "courier-50x40"
    }
    if default_template_id and default_template_id not in printable_ids:
        raise HTTPException(status_code=400, detail="默认标签模板不存在，或选择了快递单模板")
    if not default_template_id and printable_ids:
        default_template_id = next(
            str(template.get("id"))
            for template in data.templates
            if isinstance(template, dict) and template.get("id") in printable_ids
        )
    await set_setting("giffgaff_download_url", data.giffgaff_download_url)
    await set_setting("activation_tutorial_url", tutorial_url)
    await set_setting("label_templates", json.dumps(data.templates, ensure_ascii=False))
    await set_setting("default_label_template_id", default_template_id)
    if rows.get("activation_tutorial_url", DEFAULT_ACTIVATION_TUTORIAL_URL) != tutorial_url:
        await _bump_activation_page_version(rows)
    return {"ok": True}


# ── 导出 / 导入 ──

def _backup_timestamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


async def _export_backup_payload() -> dict:
    rows = await get_settings()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        customers = await db.execute_fetchall("SELECT * FROM customers ORDER BY id ASC")
        sim_codes = await db.execute_fetchall("SELECT * FROM sim_codes ORDER BY id ASC")
    return {
        "exported_at": datetime.datetime.now().isoformat(),
        "version": "1.0",
        "customers": [_customer_payload(r) for r in customers],
        "sim_codes": [dict(r) for r in sim_codes],
        "settings": {
            "app_mode": _normalize_product_type(rows.get("app_mode")),
            "moemail_url": _normalize_base_url(rows.get("moemail_url", "")),
            "giffgaff_download_url": rows.get("giffgaff_download_url", DEFAULT_GIFFGAFF_DOWNLOAD_URL),
            "activation_tutorial_url": rows.get(
                "activation_tutorial_url", DEFAULT_ACTIVATION_TUTORIAL_URL
            ),
            "activation_page_markdown": rows.get("activation_page_markdown", ""),
            "activation_page_version": _activation_page_version(rows),
            "label_templates": _load_label_templates(rows.get("label_templates", "")),
            "default_label_template_id": _default_label_template_id(
                rows, _load_label_templates(rows.get("label_templates", ""))
            ),
        },
    }


def _validate_backup_payload(data: dict) -> list[dict]:
    if data.get("version") != "1.0":
        raise HTTPException(status_code=400, detail="不支持的备份文件版本")
    customers = data.get("customers", [])
    if not isinstance(customers, list):
        raise HTTPException(status_code=400, detail="备份文件缺少 customers 列表")
    required_fields = ("id", "phone_number", "email", "activation_date", "created_at")
    for index, customer in enumerate(customers, start=1):
        if not isinstance(customer, dict):
            raise HTTPException(status_code=400, detail=f"第 {index} 条客户数据格式错误")
        missing = [field for field in required_fields if field not in customer]
        if missing:
            raise HTTPException(status_code=400, detail=f"第 {index} 条客户缺少字段：{', '.join(missing)}")
    return customers


def _validate_sim_codes_payload(data: dict) -> list[dict]:
    sim_codes = data.get("sim_codes", [])
    if sim_codes is None:
        return []
    if not isinstance(sim_codes, list):
        raise HTTPException(status_code=400, detail="备份文件 sim_codes 格式错误")
    for index, item in enumerate(sim_codes, start=1):
        if not isinstance(item, dict) or not item.get("code"):
            raise HTTPException(status_code=400, detail=f"第 {index} 条 SIM 激活码数据格式错误")
    return sim_codes


async def _restore_backup_payload(data: dict) -> dict:
    customers = _validate_backup_payload(data)
    sim_codes = _validate_sim_codes_payload(data)
    settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    safe_settings = {}

    if settings.get("app_mode") in PRODUCT_TYPES:
        safe_settings["app_mode"] = _normalize_product_type(settings["app_mode"])
    if isinstance(settings.get("moemail_url"), str):
        safe_settings["moemail_url"] = _normalize_base_url(settings["moemail_url"])
    if isinstance(settings.get("giffgaff_download_url"), str):
        safe_settings["giffgaff_download_url"] = settings["giffgaff_download_url"]
    if isinstance(settings.get("activation_tutorial_url"), str):
        tutorial_url = settings["activation_tutorial_url"].strip()
        if tutorial_url.startswith(("http://", "https://")):
            safe_settings["activation_tutorial_url"] = tutorial_url
    if isinstance(settings.get("activation_page_markdown"), str):
        safe_settings["activation_page_markdown"] = settings["activation_page_markdown"]
    if any(key in settings for key in (
        "activation_tutorial_url", "activation_page_markdown", "activation_page_version"
    )):
        try:
            safe_settings["activation_page_version"] = str(
                max(1, int(settings.get("activation_page_version") or 1)) + 1
            )
        except (TypeError, ValueError):
            safe_settings["activation_page_version"] = "2"
    if "label_templates" in settings:
        label_templates = settings["label_templates"]
        if not isinstance(label_templates, list):
            raise HTTPException(status_code=400, detail="标签模板数据格式错误")
        safe_settings["label_templates"] = json.dumps(label_templates, ensure_ascii=False)
        printable_ids = {
            str(template.get("id"))
            for template in label_templates
            if isinstance(template, dict)
            and template.get("id")
            and template.get("id") != "courier-50x40"
        }
        configured_default = settings.get("default_label_template_id")
        if isinstance(configured_default, str) and configured_default in printable_ids:
            safe_settings["default_label_template_id"] = configured_default
        elif printable_ids:
            safe_settings["default_label_template_id"] = next(
                str(template.get("id"))
                for template in label_templates
                if isinstance(template, dict) and template.get("id") in printable_ids
            )

    async with aiosqlite.connect(DATABASE_PATH) as db:
        try:
            await db.execute("BEGIN")
            await db.execute("DELETE FROM customers")
            await db.execute("DELETE FROM sim_codes")
            for sim in sim_codes:
                await db.execute(
                    """INSERT INTO sim_codes
                       (id, code, status, customer_id, notes, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        sim.get("id"),
                        _normalize_sim_code(sim.get("code")),
                        _normalize_sim_code_status(sim.get("status")),
                        sim.get("customer_id"),
                        normalize_optional_text(sim.get("notes")),
                        sim.get("created_at") or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        sim.get("updated_at") or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
            for c in customers:
                restore_values = {
                    "id": c["id"],
                    "product_type": _normalize_product_type(c.get("product_type")),
                    "phone_number": normalize_optional_text(c.get("phone_number")),
                    "email": c["email"],
                    "shipping_address": normalize_optional_text(c.get("shipping_address")),
                    "phone_status": _normalize_phone_status(c.get("phone_status") or c.get("shipping_status")),
                    "courier_company": normalize_optional_text(c.get("courier_company")),
                    "tracking_number": normalize_optional_text(c.get("tracking_number")),
                    "courier_order_code": normalize_optional_text(c.get("courier_order_code")),
                    "courier_print_data": normalize_optional_text(c.get("courier_print_data")),
                    "activation_date": c["activation_date"],
                    "moemail_id": c.get("moemail_id"),
                    "moemail_address": c.get("moemail_address"),
                    "share_link": _normalize_share_link(c.get("share_link")),
                    "is_moemail_auto": c.get("is_moemail_auto", 0),
                    "email_provider_id": c.get("email_provider_id"),
                    "email_account_id": c.get("email_account_id"),
                    "email_provider_domain": c.get("email_provider_domain"),
                    "sim_code_id": c.get("sim_code_id"),
                    "sim_activation_code": _normalize_sim_code(c.get("sim_activation_code")),
                    "public_token": c.get("public_token"),
                    "public_version": max(1, int(c.get("public_version") or 1)),
                    "first_name": normalize_optional_text(c.get("first_name")),
                    "last_name": normalize_optional_text(c.get("last_name")),
                    "address": normalize_optional_text(c.get("address")),
                    "city": normalize_optional_text(c.get("city")),
                    "postcode": normalize_optional_text(c.get("postcode")),
                    "payment_changed_at": c.get("payment_changed_at"),
                    "payment_updated_at": c.get("payment_updated_at"),
                    "payment_last_checked_at": c.get("payment_last_checked_at"),
                    "esim_raw_code": normalize_optional_text(c.get("esim_raw_code")),
                    "activation_status": _normalize_activation_status(c.get("activation_status")),
                    "activation_error": normalize_optional_text(c.get("activation_error")),
                    "activated_at": c.get("activated_at"),
                    "ctexcel_order_number": normalize_optional_text(c.get("ctexcel_order_number")),
                    "ctexcel_transaction_amount": normalize_optional_text(c.get("ctexcel_transaction_amount")),
                    "ctexcel_referral_code": normalize_optional_text(c.get("ctexcel_referral_code")),
                    "ctexcel_referral_link": normalize_optional_text(c.get("ctexcel_referral_link")),
                    "ctexcel_last_checked_at": c.get("ctexcel_last_checked_at"),
                    "created_at": c["created_at"],
                }
                restore_columns = list(restore_values)
                placeholders = ", ".join("?" for _ in restore_columns)
                await db.execute(
                    f"""INSERT INTO customers ({", ".join(restore_columns)})
                        VALUES ({placeholders})""",
                    tuple(restore_values[column] for column in restore_columns),
                )
            for key, value in safe_settings.items():
                await db.execute(
                    """INSERT INTO settings (key, value) VALUES (?, ?)
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                    (key, value),
                )
            await db.commit()
        except Exception as exc:
            await db.rollback()
            raise HTTPException(status_code=400, detail=f"恢复失败：{exc}") from exc
    return {"customers_restored": len(customers), "sim_codes_restored": len(sim_codes), "settings_restored": len(safe_settings)}


@app.get("/api/export")
async def export_all():
    data = await _export_backup_payload()
    json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"giffgaff_backup_{_backup_timestamp()}.json"
    return StreamingResponse(iter([json_bytes]), media_type="application/json",
                           headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.post("/api/import", status_code=200)
async def import_backup(file: UploadFile = File(...)):
    if not file.filename.endswith(".json"):
        raise HTTPException(status_code=400, detail="只支持 .json 文件")
    contents = await file.read()
    try:
        data = json.loads(contents)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="文件格式错误")
    restored = await _restore_backup_payload(data)
    return {"ok": True, **restored}


# ── 前端静态页面 ──

from public_routes import router as public_router
app.include_router(public_router)

@app.get("/")
async def serve_index():
    return RedirectResponse(url="/index.html")


if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")
