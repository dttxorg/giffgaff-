from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    wait,
)
import contextlib
from pathlib import Path
import re
import shutil
import threading
import tempfile
import time
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit
import uuid

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from .api import AdminApi, ApiError
from .config import (
    AppConfig,
    FREECARD_APPLICATION_URL,
    PAYMENT_METHOD_ALIPAY,
    PAYMENT_METHOD_WECHAT,
    PURCHASE_ROUTE_50GB,
    PURCHASE_ROUTE_FREECARD,
    RegistrationDefaults,
    app_config_dir,
    is_qg_proxy_api_url,
)
from .proxy import (
    BrowserProxyRoute,
    ProxyError,
    ProxyPoolRotator,
    browser_compatible_proxy,
    masked_proxy_label,
    prepare_proxy,
    probe_proxy_endpoint,
    resolve_proxy,
)
from .telegram import TelegramError, TelegramNotifier


LogCallback = Callable[[str], None]
StageCallback = Callable[[str], None]
CustomerCallback = Callable[[dict[str, Any]], None]

STALE_BROWSER_PROFILE_SECONDS = 24 * 60 * 60
AUTOMATION_STALL_TIMEOUT_MS = 20_000
BROWSER_STARTUP_TIMEOUT_MS = AUTOMATION_STALL_TIMEOUT_MS
BROWSER_SLOW_MO_MAX_MS = 250
PAGE_READY_STABLE_SECONDS = 0.35
PAGE_PROGRESS_POLL_SECONDS = 0.2
PAGE_CLICK_RETRY_SECONDS = 2.0
PAGE_CLICK_TIMEOUT_MS = 5_000
# The CTExcel plan page can acknowledge a click before its SPA route and
# detail DOM settle.  Keep that transition on a wider idle budget so three
# independent browsers do not mistake a slow response for a dead page.
PLAN_DETAILS_STALL_TIMEOUT_MS = 45_000
PAYMENT_PAGE_STALL_TIMEOUT_MS = 45_000
PAYMENT_TERMS_BIND_TIMEOUT_MS = 1_500
VERIFICATION_CODE_CACHE_SECONDS = 180
VERIFICATION_FEEDBACK_TIMEOUT_SECONDS = 5.0
TUNNEL_BROWSER_STAGGER_SECONDS = 5
TUNNEL_BROWSER_STAGGER_MAX_SECONDS = 20
PURCHASE_LIMIT_MARKERS = (
    "purchase limit",
    "purchase_limit",
    "buy limit",
    "购买上限",
    "购买限制",
    "购买次数",
    "超出限额",
    "达到上限",
)
CTEXCEL_ALLOWED_HOSTS = frozenset({"ctexcel.com", "www.ctexcel.com"})
PAYMENT_GATEWAY_HOSTS = frozenset({"na.gateway.spring.citi.com"})

SELECT_50GB_PLAN_SCRIPT = r"""() => {
  const norm = value => String(value || '').replace(/\s+/g, '');
  const nodes = Array.from(document.querySelectorAll('*'));
  const anchors = nodes.filter(el =>
    el.children.length === 0 && norm(el.textContent) === '50GB'
  );
  for (const anchor of anchors) {
    let card = anchor;
    for (
      let depth = 0;
      depth < 9 && card;
      depth += 1, card = card.parentElement
    ) {
      const text = norm(card.innerText);
      if (!text.includes('£11.9/30天') || !text.includes('立即订购')) {
        continue;
      }
      const target = Array.from(card.querySelectorAll('*')).find(el =>
        norm(el.textContent) === '立即订购'
      );
      if (target) {
        target.click();
        return true;
      }
    }
  }
  return false;
}"""

PLAN_DETAILS_READY_SCRIPT = r"""() => {
  const detail = document.querySelector('.simcarddetails');
  if (!detail) return false;
  const text = String(detail.innerText || '').replace(/\s+/g, '');
  const hasNext = Array.from(
    detail.querySelectorAll('button,[role="button"]')
  ).some(button =>
    String(button.innerText || button.textContent || '')
      .replace(/\s+/g, '') === '下一步'
  );
  return text.includes('SIM卡信息')
    && text.includes('已选套餐')
    && text.includes('50GB')
    && hasNext;
}"""

ALIPAY_GATEWAY_SELECT_SCRIPT = r"""() => {
  const norm = value => String(value || '')
    .replace(/\s+/g, '')
    .toLowerCase();
  const visible = element => {
    if (!element || element.hidden) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && Number(style.opacity || 1) > 0
      && rect.width > 0
      && rect.height > 0;
  };
  const selected = element => {
    const nodes = [element, element?.parentElement, element?.closest?.(
      'button,[role="button"],a,label,li'
    )].filter(Boolean);
    return nodes.some(node =>
      node.getAttribute?.('aria-checked') === 'true'
      || node.getAttribute?.('aria-selected') === 'true'
      || node.classList?.contains('selected')
      || node.classList?.contains('active')
      || node.classList?.contains('checked')
      || node.classList?.contains('is-active')
      || node.classList?.contains('is-checked')
    );
  };
  const candidates = Array.from(document.querySelectorAll(
    'button,[role="button"],a,label,li,div,[class*="pay"],[class*="method"],'
      + '[class*="option"]'
  )).filter(visible);
  const exact = candidates.filter(element =>
    norm(element.innerText || element.textContent) === '支付宝'
  );
  const fallback = candidates.filter(element => {
    const text = norm(element.innerText || element.textContent);
    return text.includes('支付宝') && text.length <= 24;
  });
  const option = exact[exact.length - 1] || fallback[fallback.length - 1];
  if (!option) return {found: false, selected: false};
  if (!selected(option)) {
    const target = option.closest(
      'button,[role="button"],a,label,li'
    ) || option;
    target.click();
    return {found: true, selected: false, clicked: true};
  }
  return {found: true, selected: true, clicked: false};
}"""

ALIPAY_GATEWAY_PAY_SCRIPT = r"""(expected) => {
  const norm = value => String(value || '')
    .replace(/\s+/g, '')
    .toLowerCase();
  const visible = element => {
    if (!element || element.hidden) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && Number(style.opacity || 1) > 0
      && rect.width > 0
      && rect.height > 0;
  };
  const amount = norm(expected);
  const expectedNumber = Number.parseFloat(amount);
  const candidates = Array.from(document.querySelectorAll(
    'button,[role="button"],a,[type="submit"],[class*="button"],[class*="btn"]'
  )).filter(visible);
  const matches = candidates.filter(element => {
    const text = norm(element.innerText || element.textContent);
    const paymentLabel = text.includes('支付') || text.includes('pay');
    const numbers = text.match(/\d+(?:\.\d{1,2})?/g) || [];
    const hasAmount = !amount
      || text.includes(amount)
      || (
        Number.isFinite(expectedNumber)
        && numbers.some(value => Number.parseFloat(value) === expectedNumber)
      );
    return paymentLabel && hasAmount && !text.includes('返回');
  });
  const button = matches[0];
  if (!button) return {found: false, enabled: false};
  const disabled = Boolean(
    button.disabled
    || button.getAttribute('aria-disabled') === 'true'
    || button.classList?.contains('disabled')
    || button.classList?.contains('is-disabled')
  );
  if (disabled) return {found: true, enabled: false};
  button.click();
  return {
    found: true,
    enabled: true,
    text: String(button.innerText || button.textContent || '')
  };
}"""

ALIPAY_QR_READY_SCRIPT = r"""() => {
  const text = String(document.body?.innerText || '')
    .replace(/\s+/g, '')
    .toLowerCase();
  const marker = [
    '扫一扫',
    '扫码',
    '扫描二维码',
    '二维码',
    'scanqrcode',
    'scanwithalipay',
  ].some(value => text.includes(value));
  const visible = element => {
    if (!element || element.hidden) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && Number(style.opacity || 1) > 0
      && rect.width >= 120
      && rect.height >= 120
      && rect.width / rect.height >= 0.7
      && rect.width / rect.height <= 1.45;
  };
  const qr = Array.from(document.querySelectorAll(
    'img,canvas,svg,[class*="qr"],[id*="qr"]'
  )).some(visible);
  return marker && qr;
}"""
PROXY_BROWSER_RETRY_ATTEMPTS = 3
PROXY_BROWSER_ERROR_LABELS = (
    ("proxy authentication required", "HTTP ERROR 407（代理认证失败）"),
    (
        "err_tunnel_connection_failed",
        "ERR_TUNNEL_CONNECTION_FAILED（代理隧道建立失败）",
    ),
    (
        "err_proxy_connection_failed",
        "ERR_PROXY_CONNECTION_FAILED（代理连接失败）",
    ),
)


def cleanup_stale_browser_profiles(
    profile_root: Path,
    *,
    now: Optional[float] = None,
    stale_after_seconds: int = STALE_BROWSER_PROFILE_SECONDS,
) -> int:
    """Remove abandoned per-order profiles without touching active runs."""
    current_time = time.time() if now is None else float(now)
    removed = 0
    for candidate in profile_root.glob("order-*"):
        try:
            if not candidate.is_dir():
                continue
            age = current_time - candidate.stat().st_mtime
            if age < max(60, int(stale_after_seconds)):
                continue
            shutil.rmtree(candidate)
            removed += 1
        except OSError:
            continue
    return removed


def remove_browser_profile(profile_dir: Path) -> bool:
    """Retry removal because Chrome may release Windows files slightly late."""
    for attempt in range(4):
        try:
            shutil.rmtree(profile_dir)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if attempt >= 3:
                return False
            time.sleep(0.25 * (attempt + 1))
    return False


def diagnostic_response_excerpt(text: str, *, limit: int = 1600) -> str:
    """Compact response bodies and redact common credential-shaped fields."""
    compact = " ".join(str(text or "").split())
    compact = re.sub(
        r"(?i)(auth(?:key|pwd)|password|token|authorization)"
        r"([\s\"'=:\\]+)([^\s,;\"'}]+)",
        r"\1\2<redacted>",
        compact,
    )
    return compact[: max(100, int(limit))]


def proxy_browser_error_reason(*values: Any) -> str:
    """识别浏览器代理认证和隧道错误，返回适合日志显示的原因。"""
    evidence = "\n".join(str(value or "") for value in values).lower()
    if re.search(r"(?<!\d)407(?!\d)", evidence):
        return "HTTP ERROR 407（代理认证失败）"
    for marker, label in PROXY_BROWSER_ERROR_LABELS:
        if marker in evidence:
            return label
    return ""


def browser_startup_snapshot_is_blank(snapshot: dict[str, Any]) -> bool:
    """Treat an empty viewport or a lone loading label as a blank startup page."""
    text = re.sub(r"\s+", "", str(snapshot.get("text") or "")).lower()
    loading_only = text in {
        "",
        "loading",
        "loading...",
        "加载中",
        "加载中...",
        "加载中…",
        "正在加载",
        "正在加载...",
        "正在加载…",
    }
    try:
        visible_content = int(snapshot.get("visible_content") or 0)
    except (TypeError, ValueError):
        visible_content = 0
    return loading_only and visible_content == 0


def page_progress_fingerprint(snapshot: dict[str, Any]) -> tuple[Any, ...]:
    """Return only meaningful page state used by the idle watchdog."""
    return (
        str(snapshot.get("url") or ""),
        str(snapshot.get("ready_state") or ""),
        bool(snapshot.get("loading")),
        str(snapshot.get("text_signature") or ""),
        str(snapshot.get("field_signature") or ""),
        int(snapshot.get("visible_content") or 0),
        int(snapshot.get("field_count") or 0),
        int(snapshot.get("page_count") or 0),
    )


def is_ctexcel_url(value: Any) -> bool:
    """Require HTTPS and the official CTExcel host for browser entry pages."""
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower() in CTEXCEL_ALLOWED_HOSTS
    )


def is_payment_gateway_url(value: Any) -> bool:
    """Allow only CTExcel or its configured hosted payment providers."""
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme.lower() == "https"
        and (
            host in CTEXCEL_ALLOWED_HOSTS
            or host in PAYMENT_GATEWAY_HOSTS
            or host == "alipay.com"
            or host.endswith(".alipay.com")
        )
    )


def page_url_matches_path(value: Any, expected_path: str) -> bool:
    """Match a SPA destination without depending on query parameters."""
    try:
        actual = urlsplit(str(value or "")).path.rstrip("/").lower()
    except ValueError:
        return False
    expected = str(expected_path or "").rstrip("/").lower()
    if not expected:
        return False
    actual_parts = actual.strip("/").split("/") if actual.strip("/") else []
    expected_parts = (
        expected.strip("/").split("/")
        if expected.strip("/")
        else []
    )
    if not expected_parts or len(expected_parts) > len(actual_parts):
        return False
    width = len(expected_parts)
    return any(
        actual_parts[index : index + width] == expected_parts
        for index in range(len(actual_parts) - width + 1)
    )


def tunnel_browser_start_delay(worker_slot: int) -> int:
    """Stagger shared-tunnel browser starts to avoid one simultaneous burst."""
    position = max(1, int(worker_slot)) - 1
    return min(
        TUNNEL_BROWSER_STAGGER_MAX_SECONDS,
        position * TUNNEL_BROWSER_STAGGER_SECONDS,
    )


def is_wechat_payment_url(value: Any, purchase_route: str) -> bool:
    """Match either payment route without depending on query parameters."""
    try:
        path = urlsplit(str(value or "")).path.rstrip("/").lower()
    except ValueError:
        return False
    expected = (
        "/freecard/buycardwx"
        if purchase_route == PURCHASE_ROUTE_FREECARD
        else "/buycard/buycardwx"
    )
    return path.endswith(expected)


def payment_method_is_alipay(value: Any) -> bool:
    return str(value or "").strip().lower() == PAYMENT_METHOD_ALIPAY


ORDER_PATTERN = re.compile(
    r"\b(?:ORDER\d{12,}|ORDERSUK\d{12,})\b",
    re.I,
)


def payment_page_content_is_ready(page_text: str) -> bool:
    """Recognize a rendered WeChat QR page even if the SPA URL is unchanged."""
    text = str(page_text or "")
    compact = re.sub(r"\s+", "", text).lower()
    has_qr_prompt = any(
        marker in compact
        for marker in (
            "请使用微信扫描二维码",
            "扫描二维码以完成支付",
            "微信付款二维码",
            "scantheqrcodewithwechat",
            "wechatpaymentqrcode",
        )
    )
    return has_qr_prompt and bool(ORDER_PATTERN.search(text))


PAGE_PROGRESS_SNAPSHOT_SCRIPT = """() => {
  const visible = element => {
    if (!element || element.hidden) return false;
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && Number(style.opacity || 1) > 0
      && rect.width > 0
      && rect.height > 0;
  };
  const signature = value => {
    const text = String(value || '');
    let hash = 2166136261;
    for (let index = 0; index < text.length; index += 1) {
      hash ^= text.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return `${text.length}:${hash >>> 0}`;
  };
  const body = document.body;
  const loadingSelectors = [
    '.el-loading-mask',
    '.el-loading-spinner',
    '[class*="loading-mask"]',
    '[class*="loadingMask"]'
  ];
  const loading = loadingSelectors.some(selector =>
    Array.from(document.querySelectorAll(selector)).some(visible)
  );
  const fields = body
    ? Array.from(body.querySelectorAll('input,select,textarea'))
    : [];
  const fieldState = fields.slice(0, 80).map(element => [
    element.tagName,
    element.getAttribute('placeholder') || '',
    element.getAttribute('type') || '',
    element.disabled ? 'disabled' : 'enabled',
    String(element.value || '').length
  ].join(':')).join('|');
  const visibleContent = body
    ? Array.from(body.querySelectorAll(
        'a,button,input,select,textarea,img,svg,canvas,video,iframe,'
        + '[role="button"],[role="dialog"]'
      )).filter(visible).length
    : 0;
  return {
    url: location.href,
    ready_state: document.readyState,
    loading,
    text_signature: signature(body?.innerText || ''),
    field_signature: signature(fieldState),
    visible_content: visibleContent,
    field_count: fields.length
  };
}"""
COOKIE_CONSENT_WATCHER_SCRIPT = r"""(() => {
  if (window.__ctexcelConsentWatcherInstalled) return;
  window.__ctexcelConsentWatcherInstalled = true;
  const normalized = value => String(value || '')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
  const visible = node => {
    if (!node || node.hidden) return false;
    const style = getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && Number(style.opacity || 1) > 0
      && rect.width > 0
      && rect.height > 0;
  };
  const roots = () => {
    const values = [document];
    for (let index = 0; index < values.length; index += 1) {
      for (const node of values[index].querySelectorAll('*')) {
        if (node.shadowRoot && !values.includes(node.shadowRoot)) {
          values.push(node.shadowRoot);
        }
      }
    }
    return values;
  };
  window.__ctexcelDismissCookieConsent = () => {
    const host = document.querySelector('#usercentrics-cmp-ui');
    const bodyText = normalized(document.body?.innerText);
    const consentVisible = Boolean(host)
      || bodyText.includes('隐私设置')
      || bodyText.includes('privacy settings');
    if (!consentVisible) return '';
    const preferred = new Set([
      '拒绝', '全部拒绝', 'reject', 'reject all', 'deny'
    ]);
    const fallback = new Set([
      '全部接受', '接受全部', '确认', '确认选择',
      'accept all', 'allow all', 'confirm choices'
    ]);
    const buttons = [];
    for (const root of roots()) {
      buttons.push(...root.querySelectorAll(
        'button, [role="button"], .uc-deny-button, .uc-accept-all-button'
      ));
    }
    for (const labels of [preferred, fallback]) {
      for (const button of buttons) {
        const label = normalized(
          button.innerText
          || button.textContent
          || button.getAttribute?.('aria-label')
        );
        if (visible(button) && labels.has(label)) {
          button.click();
          window.__ctexcelCookieDismissed =
            (window.__ctexcelCookieDismissed || 0) + 1;
          return preferred.has(label) ? 'reject' : 'accept';
        }
      }
    }
    return '';
  };
  const tick = () => window.__ctexcelDismissCookieConsent?.();
  const timer = setInterval(tick, 200);
  addEventListener('DOMContentLoaded', tick, {once: true});
  setTimeout(() => clearInterval(timer), 60000);
})()"""
class AutomationError(RuntimeError):
    pass


class RetryableBrowserError(AutomationError):
    """The current browser must close and restart with the same customer."""

    pass


class RetryableProxyBrowserError(RetryableBrowserError):
    """当前浏览器代理连接失败，可关闭窗口并使用新连接重试。"""

    pass


class RetryableBlankPageError(RetryableBrowserError):
    """The registration entry stayed blank for the startup deadline."""

    pass


class RetryableStalledPageError(RetryableBrowserError):
    """No browser-side progress was observed before the pre-payment limit."""

    pass


@dataclass
class AutomationResult:
    customer_id: int
    email: str
    order_number: str = ""
    phone_number: str = ""
    transaction_amount: str = ""
    batch_ordinal: int = 0
    worker_slot: int = 1


@dataclass
class AutomationBatchResult:
    completed_count: int
    total_count: int
    last_result: Optional[AutomationResult] = None


def application_target(config: AppConfig) -> int:
    if not config.continuous_enabled:
        return 1
    try:
        return min(1000, max(1, int(config.continuous_count)))
    except (TypeError, ValueError):
        return 1


def registration_values_for_ordinal(
    defaults: RegistrationDefaults,
    ordinal: int,
) -> tuple[str, str]:
    """返回当前批次序号对应的联系电话和带尾号收货地址。"""
    position = max(1, int(ordinal))
    phone = defaults.contact_phone.strip()
    phone_end = defaults.contact_phone_end.strip()
    if phone_end:
        if not re.fullmatch(r"1\d{10}", phone) or not re.fullmatch(
            r"1\d{10}", phone_end
        ):
            raise AutomationError("联系电话区间应为 11 位中国手机号码")
        phone_value = int(phone) + position - 1
        if phone_value > int(phone_end) or len(str(phone_value)) != 11:
            raise AutomationError(
                f"联系电话区间不足以生成第 {position} 单"
            )
        phone = str(phone_value)

    try:
        suffix_start = int(defaults.address_suffix_start)
        suffix_end = int(defaults.address_suffix_end)
    except (TypeError, ValueError) as exc:
        raise AutomationError("地址尾号区间应为整数") from exc
    suffix = suffix_start + position - 1
    if suffix_start < 1 or suffix_end < suffix_start or suffix > suffix_end:
        raise AutomationError(
            f"地址尾号区间不足以生成第 {position} 单"
        )
    return phone, f"{defaults.chinese_address.strip()}{suffix}"


def normalize_address_text(value: str) -> str:
    """Normalize visual separators while preserving the configured address."""
    return re.sub(r"[\s,，。;；/／|]+", "", str(value or ""))


def address_region_token(value: str) -> str:
    normalized = normalize_address_text(value)
    match = re.match(
        r"^(.{2,8}?(?:特别行政区|自治区|省|市))",
        normalized,
    )
    return match.group(1) if match else ""


def append_address_suffix(detail: str, suffix: int | str) -> str:
    base = str(detail or "").strip()
    tail = str(suffix or "").strip()
    if not base or not tail:
        raise AutomationError("详细地址或地址尾号为空")
    return f"{base}{tail}"


def is_payment_success_url(value: str) -> bool:
    try:
        path = urlsplit(str(value or "")).path.rstrip("/").lower()
    except ValueError:
        return False
    return path.endswith((
        "/buycardsucceed",
        "/activitypagesuccess",
    ))


def normalize_money(value: str) -> Optional[Decimal]:
    raw = re.sub(r"[^0-9.]", "", str(value or ""))
    if not raw:
        return None
    try:
        return Decimal(raw).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def price_is_expected(page_text: str, expected: str) -> bool:
    target = normalize_money(expected)
    if target is None:
        return False
    candidates = re.findall(
        r"(?:订单金额|交易金额)\s*[:：]\s*£\s*([0-9]+(?:\.[0-9]{1,2})?)",
        page_text or "",
    )
    return any(normalize_money(candidate) == target for candidate in candidates)


def payment_page_has_expected_amount(page_text: str, expected: str) -> bool:
    """兼容旧支付页的英镑字段和新 £1 页面中的 ``(1GBP)``。"""
    if price_is_expected(page_text, expected):
        return True
    target = normalize_money(expected)
    if target is None:
        return False
    candidates = re.findall(
        r"(?:£\s*([0-9]+(?:\.[0-9]{1,2})?)|"
        r"([0-9]+(?:\.[0-9]{1,2})?)\s*GBP)",
        page_text or "",
        re.I,
    )
    return any(
        normalize_money(left or right) == target
        for left, right in candidates
    )


def coupon_rejection_message(page_text: str) -> str:
    """提取结算页对优惠码的明确拒绝提示。"""
    text = re.sub(r"\s+", "", page_text or "")
    patterns = (
        r"(优惠券不存在或已过期)",
        r"(优惠码不存在或已过期)",
        r"(优惠券已过期)",
        r"(优惠码已过期)",
        r"(优惠券无效)",
        r"(优惠码无效)",
        r"(优惠码不可用)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1)
    return ""


def parse_message_timestamp(value: Any) -> Optional[datetime]:
    """兼容邮件供应商返回的秒、毫秒、微秒时间戳和 ISO 时间。"""
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        try:
            numeric = float(text)
            magnitude = abs(numeric)
            if magnitude >= 1e14:
                numeric /= 1_000_000
            elif magnitude >= 1e11:
                numeric /= 1_000
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def assess_verification_freshness(
    result: dict[str, Any],
    *,
    baseline_message_id: str,
    requested_at: datetime,
    clock_skew_seconds: int = 60,
) -> tuple[bool, str, Optional[datetime]]:
    """只接受本次发送后出现的新验证码邮件。"""
    message_id = str(result.get("message_id") or "").strip()
    received_at = parse_message_timestamp(result.get("received_at"))
    if baseline_message_id and message_id == baseline_message_id:
        return False, "邮件 ID 与发送前相同", received_at
    earliest = requested_at.astimezone(timezone.utc) - timedelta(
        seconds=max(0, int(clock_skew_seconds))
    )
    if received_at and received_at < earliest:
        return False, "邮件收件时间早于本次请求", received_at
    if not received_at and not (
        baseline_message_id
        and message_id
        and message_id != baseline_message_id
    ):
        return False, "缺少可核验的收件时间或新邮件 ID", None
    return True, "验证码邮件属于本次请求", received_at


def recent_verification_code(
    result: dict[str, Any],
    *,
    now: Optional[datetime] = None,
    max_age_seconds: int = VERIFICATION_CODE_CACHE_SECONDS,
    clock_skew_seconds: int = 60,
) -> str:
    """Return a code only when its message has a verifiable recent timestamp."""
    code = str(result.get("code") or "").strip()
    if not result.get("found") or not re.fullmatch(r"\d{6}", code):
        return ""
    received_at = parse_message_timestamp(result.get("received_at"))
    if received_at is None:
        return ""
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = (reference - received_at).total_seconds()
    if age < -max(0, int(clock_skew_seconds)):
        return ""
    if age > max(1, int(max_age_seconds)):
        return ""
    return code


def verification_cooldown_message(value: Any) -> str:
    """Return the site's resend-cooldown notice, if present."""
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    markers = (
        "180秒之内不要重复操作",
        "180秒内不要重复操作",
        "180秒之内请勿重复操作",
        "180秒内请勿重复操作",
    )
    return next((marker for marker in markers if marker in text), "")


class CTExcelAutomation:
    """CTExcel 购买流程。

    客户和邮箱由管理端先创建；支付页生成后回写订单号和付款金额，
    手机号码与推荐资料继续由管理端后台扫描订单邮件。
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        log: LogCallback,
        stage: StageCallback,
        customer_created: CustomerCallback,
        request_key: str = "",
        reuse_pending_customer: bool = True,
        resume_customer_id: Optional[int] = None,
        worker_slot: int = 1,
        proxy_override: Optional[dict[str, str]] = None,
        proxy_provider: Optional[Callable[[], dict[str, str]]] = None,
        browser_start_barrier: Optional[threading.Barrier] = None,
        batch_ordinal: int = 1,
    ):
        self.config = config
        self.log = log
        self.stage = stage
        self.customer_created = customer_created
        self.request_key = (
            str(request_key or "").strip()
            or uuid.uuid4().hex
        )
        self.reuse_pending_customer = bool(reuse_pending_customer)
        self.resume_customer_id = (
            int(resume_customer_id)
            if resume_customer_id is not None
            else None
        )
        self.worker_slot = max(1, int(worker_slot))
        self.proxy_override = (
            dict(proxy_override) if proxy_override is not None else None
        )
        self.proxy_provider = proxy_provider
        self.browser_start_barrier = browser_start_barrier
        self.batch_ordinal = max(1, int(batch_ordinal))
        self.stop_event = threading.Event()
        self.context: Optional[BrowserContext] = None
        self.profile_dir: Optional[Path] = None
        self.network_events: list[str] = []
        self.payment_qr_reached = False
        self.payment_qr_message_id: Optional[int] = None
        self.cached_verification_customer_id: Optional[int] = None
        self.cached_verification_code = ""
        self.cached_verification_at = 0.0

    def stop(self) -> None:
        self.stop_event.set()
        if self.browser_start_barrier is not None:
            with contextlib.suppress(threading.BrokenBarrierError):
                self.browser_start_barrier.abort()

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise AutomationError("用户已停止当前流程")

    def run(self) -> AutomationResult:
        self._check_stop()
        self._validate_registration_defaults()
        route = BrowserProxyRoute(proxy=None)
        leased_customer_id: Optional[int] = None
        completed = False
        proxy_mode = self.config.proxy.mode.strip().lower()
        defer_short_proxy = proxy_mode in {"api", "tunnel"}
        if not defer_short_proxy:
            route = self._prepare_browser_route(
                resolved_proxy=self.proxy_override,
            )

        try:
            self.stage("连接客户管理")
            with AdminApi(
                self.config.server_url,
                self.config.app_password,
                retry_callback=self.log,
                sleep=self._wait_interruptibly,
            ) as api:
                api.connect()
                self.log("客户管理连接成功")
                self.stage("准备 CTExcel 客户")
                if (
                    self.reuse_pending_customer
                    and self.resume_customer_id is None
                ):
                    self._refresh_pending_customers(api)
                created = api.create_ctexcel_customer(
                    reuse_pending=self.reuse_pending_customer,
                    allow_new_after_checkpoint=(
                        self.config.continuous_enabled
                    ),
                    resume_customer_id=self.resume_customer_id,
                    request_key=self.request_key,
                )
                customer_id = int(created["customer_id"])
                leased_customer_id = customer_id
                email = str(created["email"])
                task = {
                    "customer_id": customer_id,
                    "email": email,
                    "product_type": "ctexcel",
                    "worker_slot": self.worker_slot,
                }
                self.customer_created(task)
                if created.get("reused"):
                    self.log(
                        f"已复用无手机号的待完成客户 #{customer_id}，专属邮箱：{email}"
                    )
                else:
                    self.log(
                        f"已新建 CTExcel 客户 #{customer_id}，专属邮箱：{email}"
                    )
                self._check_stop()
                # 客户/邮箱准备完成后才建立短效代理，避免后台查询消耗 IP 寿命。
                initial_proxy = self.proxy_override
                if defer_short_proxy:
                    if (
                        initial_proxy is None
                        and self.proxy_provider is not None
                    ):
                        try:
                            initial_proxy = self.proxy_provider()
                        except ProxyError as exc:
                            raise AutomationError(
                                f"青果独立 IP 提取失败：{exc}"
                            ) from exc
                    route = self._prepare_browser_route(
                        resolved_proxy=initial_proxy,
                    )
                for attempt in range(1, PROXY_BROWSER_RETRY_ATTEMPTS + 1):
                    try:
                        result = self._run_browser(
                            api,
                            customer_id,
                            email,
                            browser_proxy=route.proxy,
                            synchronize_start=(attempt == 1),
                        )
                        completed = True
                        return result
                    except RetryableBrowserError as exc:
                        route.close()
                        route = BrowserProxyRoute(proxy=None)
                        # A browser restart can follow a rejected or consumed
                        # verification code.  Revalidate through the mailbox on
                        # the next attempt instead of trusting process memory.
                        self._clear_verification_code_cache()
                        if isinstance(exc, RetryableBlankPageError):
                            self.stage("空白页超时，关闭浏览器")
                            self.log(f"{exc}；已关闭当前空白浏览器")
                        elif isinstance(exc, RetryableStalledPageError):
                            self.stage("20 秒无进展，关闭浏览器")
                            self.log(f"{exc}；已关闭当前卡住的浏览器")
                        else:
                            self.log(
                                f"代理连接失败，已关闭当前浏览器：{exc}"
                            )
                        if attempt >= PROXY_BROWSER_RETRY_ATTEMPTS:
                            self.stage("浏览器重试失败")
                            raise AutomationError(
                                "浏览器连续 "
                                f"{PROXY_BROWSER_RETRY_ATTEMPTS} 次在支付页前中断，"
                                "当前客户已保留，可重新运行继续"
                            ) from exc
                        next_attempt = attempt + 1
                        self.stage("重新准备浏览器")
                        retry_delay = min(
                            10,
                            1 + max(0, self.worker_slot - 1) * 2,
                        )
                        self.log(
                            f"{retry_delay} 秒后重新准备代理并启动第 "
                            f"{next_attempt} / {PROXY_BROWSER_RETRY_ATTEMPTS} 次；"
                            "继续使用当前客户和邮箱"
                        )
                        self._wait_interruptibly(retry_delay)
                        if (
                            self.config.proxy.mode.strip().lower() == "api"
                            and self.proxy_provider is not None
                        ):
                            try:
                                retry_override = self.proxy_provider()
                            except ProxyError as proxy_exc:
                                raise AutomationError(
                                    "青果重试节点提取失败："
                                    f"{proxy_exc}"
                                ) from proxy_exc
                        elif self.config.proxy.mode.strip().lower() == "api":
                            retry_override = None
                        else:
                            retry_override = initial_proxy
                        route = self._prepare_browser_route(
                            resolved_proxy=retry_override,
                        )
        finally:
            try:
                route.close()
            finally:
                if leased_customer_id is not None and not completed:
                    self._release_customer_lease(leased_customer_id)

    def _release_customer_lease(self, customer_id: int) -> None:
        """Best-effort release; a hard crash still falls back to lease expiry."""
        try:
            with AdminApi(
                self.config.server_url,
                self.config.app_password,
                timeout=5.0,
                retry_callback=self.log,
                retry_delays=(),
                sleep=self._wait_interruptibly,
            ) as api:
                released = api.release_ctexcel_customer(
                    customer_id,
                    request_key=self.request_key,
                )
        except Exception as exc:
            self.log(f"客户租约将在服务端超时后释放：{exc}")
            return
        if released:
            self.log("本次流程已结束，当前客户租约已释放，可立即重新运行")

    def _prepare_browser_route(
        self,
        *,
        resolved_proxy: Optional[dict[str, str]],
    ) -> BrowserProxyRoute:
        self.stage("准备浏览器代理")
        route = BrowserProxyRoute(proxy=None)
        try:
            prepared_proxy = prepare_proxy(
                self.config.proxy,
                resolved_proxy=resolved_proxy,
            )
            browser_upstream_proxy = prepared_proxy.playwright_proxy
            if prepared_proxy.public_ip:
                self.log(
                    f"当前出口公网 IP：{prepared_proxy.public_ip}"
                )
            elif prepared_proxy.public_ip_error:
                self.log(prepared_proxy.public_ip_error)
            if browser_upstream_proxy:
                source = {
                    "api": "动态提取",
                    "tunnel": "青果隧道",
                    "pool": "代理池",
                }.get(self.config.proxy.mode, "固定配置")
                self.log(
                    f"{source}代理已就绪："
                    f"{masked_proxy_label(browser_upstream_proxy)}"
                )
                route = browser_compatible_proxy(browser_upstream_proxy)
                if route.bridge:
                    probe_proxy_endpoint(route.proxy or {})
                    self.log(
                        "带认证 SOCKS5 已通过本机桥接完成端到端预检："
                        f"{masked_proxy_label(route.proxy)}"
                    )
                else:
                    self.log("浏览器将直接载入已验证的代理")
            else:
                self.log("浏览器使用直连")
            return route
        except ProxyError as exc:
            route.close()
            raise AutomationError(f"浏览器代理准备失败：{exc}") from exc

    def _refresh_pending_customers(self, api: AdminApi) -> None:
        """开始新流程前先扫描无手机号客户，避免重复建立空记录。"""
        pending = api.pending_customers()
        if not pending:
            self.log("没有无手机号的待完成客户，将新建客户")
            return
        completed_pending = [
            customer
            for customer in pending
            if (
                str(
                    customer.get("registration_confirmed_at") or ""
                ).strip()
                or str(
                    customer.get("payment_succeeded_at") or ""
                ).strip()
            )
        ]
        if completed_pending:
            self.log(
                f"{len(completed_pending)} 个账号已确认支付或注册成功，"
                "不会再次提交"
            )
        scan_targets = [
            customer
            for customer in pending
            if customer not in completed_pending
        ]
        if not scan_targets:
            return
        unpaid_order_count = sum(
            bool(str(customer.get("order_number") or "").strip())
            for customer in scan_targets
        )
        if unpaid_order_count:
            self.log(
                f"{unpaid_order_count} 个已生成订单但尚未确认支付成功的客户，"
                "将优先复用而不是新建档案"
            )
        self.log(
            f"检测到 {len(scan_targets)} 个未完成的中断客户，"
            "先扫描订单邮件"
        )
        synced_count = 0
        confirmed_count = 0
        for customer in scan_targets[:20]:
            self._check_stop()
            customer_id = int(customer.get("customer_id") or 0)
            if not customer_id:
                continue
            try:
                result = api.sync_order_info(customer_id)
            except ApiError as exc:
                self.log(f"客户 #{customer_id} 邮件扫描暂未完成：{exc}")
                continue
            if str(result.get("phone_number") or "").strip():
                synced_count += 1
                self.log(f"客户 #{customer_id} 已从订单邮件同步手机号")
            if result.get("registration_confirmed"):
                confirmed_count += 1
                self.log(
                    f"客户 #{customer_id} 已收到“【CTExcel】"
                    "您的订单已确认！”邮件，标记为注册成功并跳过复用"
                )
        if synced_count:
            self.log(f"本轮已补全 {synced_count} 个客户的手机号")
        if confirmed_count:
            self.log(
                f"本轮确认 {confirmed_count} 个账号已经注册成功，"
                "后续申请将使用新客户邮箱"
            )

    def _validate_registration_defaults(self) -> None:
        defaults = self.config.registration
        missing = []
        if not defaults.last_name.strip():
            missing.append("固定姓")
        if not defaults.first_name.strip():
            missing.append("固定名")
        if not defaults.contact_phone.strip():
            missing.append("联系电话起始号码")
        if not defaults.chinese_address.strip():
            missing.append("固定中国收货地址")
        if missing:
            raise AutomationError("请先填写：" + "、".join(missing))
        if not re.fullmatch(r"1\d{10}", defaults.contact_phone.strip()):
            raise AutomationError("联系电话起始号码应为 11 位中国手机号码")
        registration_values_for_ordinal(defaults, self.batch_ordinal)
        if self.config.purchase_route == PURCHASE_ROUTE_FREECARD:
            if not re.fullmatch(
                r"(?:\+?44)?7\d{9,10}",
                defaults.freecard_referrer.strip(),
            ):
                raise AutomationError("£1 路线推荐人号码格式错误")
        elif self.config.purchase_route != PURCHASE_ROUTE_50GB:
            raise AutomationError("请选择有效的 CTExcel 申请路线")
        if (
            self.config.purchase_route == PURCHASE_ROUTE_50GB
            and normalize_money(defaults.expected_price_gbp) is None
        ):
            raise AutomationError("预期优惠价格格式错误")

    def _run_browser(
        self,
        api: AdminApi,
        customer_id: int,
        email: str,
        *,
        browser_proxy: Optional[dict[str, str]],
        synchronize_start: bool = True,
    ) -> AutomationResult:
        self.stage("启动浏览器")
        # A retry or a caller reusing the automation object must not leave a
        # QR message from an earlier browser run behind in Telegram.
        self._delete_payment_qr("开始新的浏览器流程")
        self.payment_qr_reached = False
        with sync_playwright() as playwright:
            profile_root = Path(app_config_dir()) / "browser-runs"
            profile_root.mkdir(parents=True, exist_ok=True)
            stale_count = cleanup_stale_browser_profiles(profile_root)
            if stale_count:
                self.log(f"已清理 {stale_count} 个异常中断遗留的浏览器目录")
            self.profile_dir = Path(
                tempfile.mkdtemp(
                    prefix="order-",
                    dir=str(profile_root),
                )
            )
            launch_options: dict[str, Any] = {
                "headless": bool(self.config.headless),
                # 短效节点下优先在有效期内完成自动化步骤；
                # 仍保留少量可观察间隔，且防止旧配置恢复为 800ms。
                "slow_mo": min(
                    BROWSER_SLOW_MO_MAX_MS,
                    max(0, int(self.config.slow_mo_ms)),
                ),
                # 去掉 Chrome 的自动测试横幅和最明显的 webdriver 标记。
                "ignore_default_args": [
                    "--enable-automation",
                    "--no-sandbox",
                ],
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    (
                        "--window-position="
                        f"{40 * ((self.worker_slot - 1) % 5)},"
                        f"{35 * ((self.worker_slot - 1) // 5)}"
                    ),
                ],
            }
            channel = (self.config.browser_channel or "").strip().lower()
            if channel and channel != "chromium":
                launch_options["channel"] = channel
            if browser_proxy:
                launch_options["proxy"] = browser_proxy
            try:
                proxy_mode = self.config.proxy.mode.strip().lower()
                if synchronize_start and proxy_mode == "tunnel":
                    stagger_seconds = tunnel_browser_start_delay(
                        self.worker_slot
                    )
                    if stagger_seconds:
                        self.stage("隧道浏览器错峰启动")
                        self.log(
                            f"共享隧道线程在建立浏览器前错峰 "
                            f"{stagger_seconds} 秒，不消耗已建立连接的寿命"
                        )
                        self._wait_interruptibly(stagger_seconds)
                self.context = playwright.chromium.launch_persistent_context(
                    str(self.profile_dir),
                    **launch_options,
                )
                with contextlib.suppress(Exception):
                    self.context.clear_cookies()
                self.context.add_init_script(
                    """
                    Object.defineProperty(
                      Navigator.prototype,
                      'webdriver',
                      {get: () => undefined, configurable: true}
                    );
                    """
                )
                self.context.add_init_script(COOKIE_CONSENT_WATCHER_SCRIPT)
                self.log(
                    "已启用浏览器兼容模式：每单使用独立临时配置，"
                    "并移除 Chrome/Edge 的自动测试标记"
                )
            except BaseException:
                self._cleanup_browser_resources()
                raise
            page: Optional[Page] = None
            try:
                page = self.context.pages[0] if self.context.pages else self.context.new_page()
                with contextlib.suppress(Exception):
                    page.evaluate(
                        """() => {
                          localStorage.clear();
                          sessionStorage.clear();
                          if ('caches' in window) {
                            caches.keys().then(keys =>
                              Promise.all(keys.map(key => caches.delete(key)))
                            );
                          }
                        }"""
                    )
                self._attach_page_diagnostics(page)
                page.set_default_timeout(
                    self._automation_step_timeout_ms()
                )
                page.set_default_navigation_timeout(
                    self._automation_wait_timeout_ms()
                )
                if browser_proxy:
                    if self.config.proxy.mode == "tunnel":
                        self.log(
                            "青果隧道已载入浏览器；跳过独立连通性和第三方 "
                            "IP 检测，直接进入注册"
                        )
                    else:
                        self.log(
                            "青果代理已通过 CTExcel 端口预检；"
                            "跳过第三方 IP 检测并直接进入注册"
                        )
                if (
                    synchronize_start
                    and proxy_mode not in {"api", "tunnel"}
                    and self.browser_start_barrier is not None
                ):
                    self.stage("等待并发窗口就绪")
                    self.log("浏览器已就绪，等待首批并发窗口")
                    try:
                        self.browser_start_barrier.wait(timeout=90)
                        self.log("首批窗口已全部就绪，同时开始操作")
                    except threading.BrokenBarrierError:
                        self.log("部分窗口未按时就绪，当前线程继续操作")
                elif synchronize_start and proxy_mode == "api":
                    self.log("短效节点不等待并发屏障，浏览器就绪后立即操作")
                if self.config.purchase_route == PURCHASE_ROUTE_FREECARD:
                    self._start_freecard_application(page)
                else:
                    self._select_plan(page)
                    self._configure_sim(page)
                self._fill_customer_info(page, api, customer_id, email)
                self._confirm_order(page)
                page, pending = self._open_payment(
                    page,
                    api=api,
                    customer_id=customer_id,
                )
                if pending.get("payment_method") == PAYMENT_METHOD_WECHAT:
                    self._push_payment_qr(
                        page,
                        customer_id=customer_id,
                        email=email,
                        pending_order=pending,
                    )
                result = self._wait_for_payment_success(
                    page,
                    api=api,
                    customer_id=customer_id,
                    email=email,
                    pending_order=pending,
                )
                self.stage("支付成功")
                if self.config.continuous_enabled:
                    self.log(
                        "连续申请模式：本单已完成，"
                        "当前浏览器将关闭并准备下一单"
                    )
                self.log(
                    "支付成功；客户端已保存订单号和付款金额，"
                    "手机号不作为流程条件"
                )
                self._wait_interruptibly(2)
                return result
            except Exception as exc:
                self._handle_browser_failure(
                    page,
                    exc,
                    browser_proxy=browser_proxy,
                )
            finally:
                self._cleanup_browser_resources()

    def _cleanup_browser_resources(self) -> None:
        """Close a partially initialized browser and remove its profile."""
        self._delete_payment_qr(
            "用户停止" if self.stop_event.is_set() else "浏览器流程结束"
        )
        with contextlib.suppress(Exception):
            if self.context is not None:
                self.context.close()
        self.context = None
        if self.profile_dir:
            if not remove_browser_profile(self.profile_dir):
                self.log("浏览器目录仍被系统占用；下次启动会自动清理")
        self.profile_dir = None

    def _handle_browser_failure(
        self,
        page: Optional[Page],
        exc: Exception,
        *,
        browser_proxy: Optional[dict[str, str]],
    ) -> None:
        diagnostic_page = page
        try:
            page_closed = page is None or page.is_closed()
        except Exception:
            page_closed = True
        if page_closed and self.context is not None:
            with contextlib.suppress(Exception):
                open_pages = [
                    candidate
                    for candidate in self.context.pages
                    if not candidate.is_closed()
                ]
                if open_pages:
                    diagnostic_page = open_pages[-1]
        reason = self._page_proxy_error_reason(diagnostic_page, exc)
        if browser_proxy and reason:
            self.stage("代理异常，关闭浏览器")
            self.log(f"检测到可重试代理错误：{reason}")
            raise RetryableProxyBrowserError(reason) from exc
        if isinstance(exc, RetryableBrowserError):
            raise exc
        closed_error = any(
            marker in str(exc).lower()
            for marker in (
                "target page, context or browser has been closed",
                "page has been closed",
            )
        )
        if not self.payment_qr_reached and (
            isinstance(exc, PlaywrightTimeoutError)
            or closed_error
        ):
            raise RetryableStalledPageError(
                "支付页前 20 秒未出现新页面动作"
                if isinstance(exc, PlaywrightTimeoutError)
                else "支付页前浏览器页面意外关闭"
            ) from exc
        if diagnostic_page is not None:
            self._preserve_error_page(diagnostic_page, exc)
        raise exc

    def _automation_wait_timeout_ms(self) -> int:
        return min(
            AUTOMATION_STALL_TIMEOUT_MS,
            max(1_000, int(self.config.page_timeout_ms)),
        )

    def _automation_step_timeout_ms(self) -> int:
        return min(
            AUTOMATION_STALL_TIMEOUT_MS,
            max(1_000, int(self.config.step_timeout_ms)),
        )

    def _browser_startup_timeout_ms(self) -> int:
        return self._automation_wait_timeout_ms()

    def _page_startup_snapshot(self, page: Page) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "text": "",
            "visible_content": 0,
            "ready_state": "unknown",
            "title": "",
            "url": str(getattr(page, "url", "") or ""),
        }
        with contextlib.suppress(Exception):
            value = page.evaluate(
                """() => {
                  const body = document.body;
                  const visible = element => {
                    if (!element || element.hidden) return false;
                    const style = getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.display !== 'none'
                      && style.visibility !== 'hidden'
                      && Number(style.opacity || 1) > 0
                      && rect.width > 4
                      && rect.height > 4;
                  };
                  const content = body
                    ? Array.from(body.querySelectorAll(
                        'a,button,input,select,textarea,img,svg,canvas,'
                        + 'video,iframe,[role="button"]'
                      )).filter(visible).length
                    : 0;
                  return {
                    text: body?.innerText || '',
                    visible_content: content,
                    ready_state: document.readyState,
                    title: document.title || '',
                    url: location.href
                  };
                }"""
            )
            if isinstance(value, dict):
                snapshot.update(value)
        return snapshot

    def _page_progress_snapshot(self, page: Page) -> dict[str, Any]:
        """Capture URL, Loading, form and visible-DOM progress in one call."""
        snapshot: dict[str, Any] = {
            "url": str(getattr(page, "url", "") or ""),
            "ready_state": "unknown",
            "loading": False,
            "text_signature": "",
            "field_signature": "",
            "visible_content": 0,
            "field_count": 0,
            "page_count": 0,
        }
        with contextlib.suppress(Exception):
            value = page.evaluate(PAGE_PROGRESS_SNAPSHOT_SCRIPT)
            if isinstance(value, dict):
                snapshot.update(value)
        with contextlib.suppress(Exception):
            snapshot["page_count"] = len(page.context.pages)
        return snapshot

    def _page_target_state(
        self,
        page: Page,
        *,
        expected_path: str,
        ready_script: str,
    ) -> tuple[bool, str]:
        current_url = ""
        with contextlib.suppress(Exception):
            current_url = str(page.url or "")
        if (
            is_ctexcel_url(current_url)
            and page_url_matches_path(current_url, expected_path)
        ):
            return True, "目标网址已出现"
        with contextlib.suppress(Exception):
            if bool(page.evaluate(ready_script)):
                return True, "目标表单已出现"
        return False, ""

    def _wait_for_page_transition(
        self,
        page: Page,
        *,
        label: str,
        expected_path: str,
        ready_script: str,
        retry_action: Optional[Callable[[], None]] = None,
        stall_timeout_ms: Optional[int] = None,
    ) -> None:
        """Wait for a route/DOM target; the default idle budget is 20 seconds."""
        stall_ms = (
            AUTOMATION_STALL_TIMEOUT_MS
            if stall_timeout_ms is None
            else max(1_000, int(stall_timeout_ms))
        )
        stall_seconds = stall_ms / 1000
        total_seconds = max(
            stall_seconds,
            max(1_000, int(self.config.page_timeout_ms)) / 1000,
        )
        started = time.monotonic()
        last_progress_at = started
        initial_fingerprint: Optional[tuple[Any, ...]] = None
        last_fingerprint: Optional[tuple[Any, ...]] = None
        retried = False
        progress_logged = False
        self.log(f"等待进入{label}")
        while time.monotonic() - started < total_seconds:
            self._check_stop()
            reached, reason = self._page_target_state(
                page,
                expected_path=expected_path,
                ready_script=ready_script,
            )
            if reached:
                self.log(f"{label}已确认：{reason}")
                return
            try:
                page_closed = page.is_closed()
            except Exception:
                page_closed = True
            if page_closed:
                raise RetryableStalledPageError(
                    f"{label}出现前页面已关闭"
                )
            snapshot = self._page_progress_snapshot(page)
            fingerprint = page_progress_fingerprint(snapshot)
            now = time.monotonic()
            if initial_fingerprint is None:
                initial_fingerprint = fingerprint
                last_fingerprint = fingerprint
            elif fingerprint != last_fingerprint:
                last_fingerprint = fingerprint
                last_progress_at = now
                if not progress_logged:
                    self.log(f"{label}正在加载，已检测到页面进度")
                    progress_logged = True
            elif (
                retry_action is not None
                and not retried
                and now - started >= PAGE_CLICK_RETRY_SECONDS
                and fingerprint == initial_fingerprint
            ):
                retried = True
                self.log("首次点击未产生页面变化，自动重试一次")
                try:
                    retry_action()
                except AutomationError:
                    raise
                except Exception as exc:
                    self.log(f"点击重试未返回：{type(exc).__name__}: {exc}")
                last_progress_at = time.monotonic()
            if now - last_progress_at >= stall_seconds:
                if stall_ms == AUTOMATION_STALL_TIMEOUT_MS:
                    message = (
                        f"{label}连续 20 秒没有 URL、Loading 或表单变化"
                    )
                else:
                    message = (
                        f"{label}连续 {int(stall_seconds)} 秒没有 URL、"
                        "Loading 或表单变化"
                    )
                raise RetryableStalledPageError(
                    message
                )
            self._wait_interruptibly(PAGE_PROGRESS_POLL_SECONDS)
        raise RetryableStalledPageError(
            f"{label}持续变化但超过 {int(total_seconds)} 秒仍未完成"
        )

    def _open_registration_entry(
        self,
        page: Page,
        url: str,
        *,
        label: str,
        ready_script: str,
    ) -> None:
        """Open an entry page and require useful content within 30 seconds."""
        if not is_ctexcel_url(url):
            raise AutomationError(
                "CTExcel 申请入口必须使用 https://www.ctexcel.com 域名"
            )
        timeout_ms = self._browser_startup_timeout_ms()
        timeout_seconds = max(1, timeout_ms // 1000)
        started = time.monotonic()
        self.log(f"{label}已启用 {timeout_seconds} 秒空白页看门狗")
        try:
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            raise RetryableBlankPageError(
                f"{label}打开超过 {timeout_seconds} 秒仍未完成"
            ) from exc
        self._raise_if_proxy_error_page(page, response)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        remaining_ms = max(1, timeout_ms - elapsed_ms)
        try:
            page.wait_for_function(
                ready_script,
                timeout=remaining_ms,
            )
        except PlaywrightTimeoutError as exc:
            self._raise_if_proxy_error_page(page, response)
            snapshot = self._page_startup_snapshot(page)
            if browser_startup_snapshot_is_blank(snapshot):
                raise RetryableBlankPageError(
                    f"{label}打开 {timeout_seconds} 秒仍是一片空白"
                ) from exc
            title = str(snapshot.get("title") or "").strip()
            raise AutomationError(
                f"{label}在 {timeout_seconds} 秒内未出现预期内容；"
                f"当前页面非空白（标题：{title or '无'}）"
            ) from exc
        self.log(f"{label}已加载出有效页面内容")

    def _page_proxy_error_reason(
        self,
        page: Optional[Page],
        *values: Any,
    ) -> str:
        page_title = ""
        page_text = ""
        if page is not None:
            with contextlib.suppress(Exception):
                page_title = page.title()
            with contextlib.suppress(Exception):
                page_text = page.locator("body").inner_text(timeout=1000)
        reason = proxy_browser_error_reason(
            *values,
            page_title,
            page_text,
            *self.network_events[-20:],
        )
        return reason

    def _raise_if_proxy_error_page(
        self,
        page: Page,
        response: Any = None,
    ) -> None:
        status = ""
        if response is not None:
            with contextlib.suppress(Exception):
                status = f"HTTP {int(response.status)}"
        reason = self._page_proxy_error_reason(page, status)
        if reason:
            raise RetryableProxyBrowserError(reason)

    def _record_network_event(self, message: str) -> None:
        self.network_events.append(message)
        if len(self.network_events) > 200:
            del self.network_events[:-200]

    def _attach_page_diagnostics(self, page: Page) -> None:
        self.network_events = []

        def on_request_failed(request: Any) -> None:
            parsed = urlsplit(str(getattr(request, "url", "") or ""))
            if not (parsed.hostname or "").lower().endswith(
                "ctexcel.com"
            ):
                return
            failure = str(getattr(request, "failure", "") or "unknown")
            self._record_network_event(
                f"FAILED {getattr(request, 'method', 'GET')} "
                f"{parsed.path} · {failure}"
            )

        def on_request_finished(request: Any) -> None:
            parsed = urlsplit(str(getattr(request, "url", "") or ""))
            if not (parsed.hostname or "").lower().endswith(
                "ctexcel.com"
            ):
                return
            with contextlib.suppress(Exception):
                response = request.response()
                if response is None:
                    return
                status = int(response.status)
                body = ""
                with contextlib.suppress(Exception):
                    body = diagnostic_response_excerpt(response.text())
                has_limit_marker = any(
                    marker in body.lower()
                    for marker in PURCHASE_LIMIT_MARKERS
                )
                if status >= 400 or has_limit_marker:
                    suffix = f" · BODY {body}" if body else ""
                    self._record_network_event(
                        f"HTTP {status} "
                        f"{getattr(request, 'method', 'GET')} "
                        f"{parsed.path}{suffix}"
                    )

        def on_page_error(error: Any) -> None:
            self._record_network_event(
                "PAGE_ERROR " + str(error).replace("\n", " ")[:500]
            )

        page.on("requestfailed", on_request_failed)
        page.on("requestfinished", on_request_finished)
        page.on("pageerror", on_page_error)

    def _preserve_error_page(self, page: Page, exc: Exception) -> None:
        """保存错误现场，并在可视模式下短暂保留浏览器供人工检查。"""
        diagnostics = Path(app_config_dir()) / "diagnostics"
        diagnostics.mkdir(parents=True, exist_ok=True)
        stamp = (
            f"{time.strftime('%Y%m%d-%H%M%S')}-"
            f"w{self.worker_slot}-{uuid.uuid4().hex[:8]}"
        )
        screenshot_path = diagnostics / f"error-{stamp}.png"
        html_path = diagnostics / f"error-{stamp}.html"
        network_path = diagnostics / f"error-{stamp}-network.txt"
        with contextlib.suppress(Exception):
            page.screenshot(path=str(screenshot_path), full_page=True)
        with contextlib.suppress(Exception):
            html_path.write_text(page.content(), encoding="utf-8")
        with contextlib.suppress(Exception):
            network_path.write_text(
                "\n".join(self.network_events) or "No captured events",
                encoding="utf-8",
            )

        self.stage("错误现场已保留")
        self.log(f"流程错误：{exc}")
        self.log(
            f"诊断文件：{diagnostics}（截图、HTML、network.txt）"
        )
        hold_seconds = max(0, int(self.config.error_browser_hold_seconds))
        if self.config.headless or hold_seconds == 0:
            return
        self.log(
            f"浏览器将保留最多 {hold_seconds} 秒；检查页面后关闭浏览器或点击停止。"
        )
        deadline = time.monotonic() + hold_seconds
        while time.monotonic() < deadline and not self.stop_event.is_set():
            try:
                if page.is_closed():
                    break
            except Exception:
                break
            time.sleep(0.5)

    def _start_freecard_application(self, page: Page) -> None:
        self.stage("选择申请路线")
        self._open_registration_entry(
            page,
            FREECARD_APPLICATION_URL,
            label="£1 领卡活动页",
            ready_script="""() => {
              const text = document.body?.innerText || '';
              return text.includes('还没选好套餐')
                && text.includes('先预存£1领卡');
            }""",
        )
        self._dismiss_cookie_consent(page)
        self._wait_for_page_ready(page, "£1 领卡活动页")
        self._click_visible_text(
            page,
            "还没选好套餐，先预存£1领卡 >",
        )

        self.stage("配置 SIM / 套餐")
        page.wait_for_function(
            """() => {
              const text = document.body?.innerText || '';
              return text.includes('SIM卡类型')
                && text.includes('订单金额')
                && text.includes('£1');
            }""",
            timeout=self._automation_wait_timeout_ms(),
        )
        self._ensure_selected_option(
            page,
            "实体SIM卡",
            exact=False,
            label="实体 SIM 卡",
        )
        self._ensure_selected_option(
            page,
            "免费随机号码",
            exact=True,
            label="免费随机号码",
        )
        page_text = page.locator("body").inner_text()
        if not price_is_expected(page_text, "1.00"):
            raise AutomationError("£1 预存领卡页面的订单金额校验失败")
        self.log("已选择预存 £1 领卡、实体 SIM、免费随机号码")
        self._click_button_and_wait_for_page(
            page,
            "下一步",
            label="£1 领卡资料页",
            expected_path="/freecard/activityPagefillInfos",
            ready_script="""() => Boolean(
              document.querySelector('input[placeholder="请填写姓"]')
              || document.querySelector('input[placeholder="请填写邮箱"]')
            )""",
        )
        self._wait_for_page_ready(page, "£1 领卡资料页")

    def _select_plan(self, page: Page) -> None:
        self.stage("选择申请路线")
        self._open_registration_entry(
            page,
            self.config.application_url,
            label="套餐列表",
            ready_script=(
                "() => document.body "
                "&& document.body.innerText.includes('50GB')"
            ),
        )
        self._dismiss_cookie_consent(page)
        self._wait_for_page_ready(page, "套餐列表")
        selected = page.evaluate(SELECT_50GB_PLAN_SCRIPT)
        if not selected:
            raise AutomationError("没有定位到 50GB / £11.9 套餐的立即订购按钮")

        self.log("50GB / £11.9 套餐按钮点击已提交，等待套餐详情")

        # This is a SPA transition: the route can lag behind the rendered
        # detail DOM, especially while three browser contexts share a link.
        # Accept the route or the specific detail DOM; a second purchase click
        # is deliberately avoided while the first navigation may be in flight.
        self._wait_for_page_transition(
            page,
            label="套餐详情",
            expected_path="/buycard/simcarddetails",
            ready_script=PLAN_DETAILS_READY_SCRIPT,
            stall_timeout_ms=PLAN_DETAILS_STALL_TIMEOUT_MS,
        )
        self._wait_for_page_ready(
            page,
            "套餐详情",
            stall_timeout_ms=PLAN_DETAILS_STALL_TIMEOUT_MS,
        )
        self.log("已选择 50GB、£11.9/30天套餐")

    def _configure_sim(self, page: Page) -> None:
        self.stage("配置 SIM / 套餐")
        self._dismiss_cookie_consent(page)
        self._wait_for_page_ready(
            page,
            "SIM 卡配置页",
            stall_timeout_ms=PLAN_DETAILS_STALL_TIMEOUT_MS,
        )
        sim_config_timeout_ms = max(
            self._automation_wait_timeout_ms(),
            PLAN_DETAILS_STALL_TIMEOUT_MS,
        )
        try:
            page.wait_for_function(
                """() => {
                  const text = document.body?.innerText || '';
                  return text.includes('SIM卡类型') && text.includes('自动续订');
                }""",
                timeout=sim_config_timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            raise RetryableStalledPageError(
                "SIM 卡配置页连续 "
                f"{sim_config_timeout_ms // 1000} 秒没有出现可操作控件"
            ) from exc
        # “实体SIM卡”与说明文字在同一个 div 中，exact=True 会得到 0 个匹配。
        self._ensure_selected_option(
            page,
            "实体SIM卡",
            exact=False,
            label="实体 SIM 卡",
        )
        self._ensure_selected_option(
            page,
            "免费随机号码",
            exact=True,
            label="免费随机号码",
        )
        self._ensure_selected_option(
            page,
            "1 个月",
            exact=True,
            label="订购周期 1 个月",
        )

        self._wait_for_page_ready(page, "自动续订开关")
        switch = self._visible_locator(
            page.locator(".el-switch"),
            "自动续订开关",
        )
        switch_class = switch.get_attribute("class") or ""
        if "is-checked" in switch_class:
            switch.click()
            self._wait_for_page_ready(page, "关闭自动续订")
            page.wait_for_function(
                """() => {
                  const root = document.querySelector('.el-switch');
                  return root && !root.classList.contains('is-checked');
                }""",
                timeout=self._automation_step_timeout_ms(),
            )
        switch_class = switch.get_attribute("class") or ""
        if "is-checked" in switch_class:
            raise AutomationError("自动续订仍处于开启状态")
        self.log("实体 SIM、免费随机号码、1个月、1张，自动续订已关闭")
        self._click_button_and_wait_for_page(
            page,
            "下一步",
            label="客户资料页",
            expected_path="/buycard/fillinfos",
            ready_script="""() => Boolean(
              document.querySelector('input[placeholder="请填写姓"]')
              || document.querySelector('input[placeholder="请填写邮箱"]')
            )""",
        )
        self._wait_for_page_ready(page, "客户资料页")

    def _dismiss_cookie_consent(self, page: Page) -> None:
        """关闭 Usercentrics；页面内监视器会继续处理延迟弹出。"""
        started = time.monotonic()
        deadline = started + 5
        quiet_deadline = started + 0.8
        saw_dialog = False
        while time.monotonic() < deadline:
            self._check_stop()
            try:
                state = page.evaluate(
                    """() => {
                      const action = window.__ctexcelDismissCookieConsent?.()
                        || '';
                      const host = document.querySelector(
                        '#usercentrics-cmp-ui'
                      );
                      const text = String(
                        document.body?.innerText || ''
                      ).toLowerCase();
                      const hostVisible = (() => {
                        if (!host) return false;
                        const style = getComputedStyle(host);
                        const rect = host.getBoundingClientRect();
                        return style.display !== 'none'
                          && style.visibility !== 'hidden'
                          && Number(style.opacity || 1) > 0
                          && rect.width > 0
                          && rect.height > 0;
                      })();
                      const visible = hostVisible
                        || text.includes('隐私设置')
                        || text.includes('privacy settings');
                      return {action, visible};
                    }"""
                )
            except Exception:
                if page.is_closed():
                    return
                page.wait_for_timeout(200)
                continue
            action = str((state or {}).get("action") or "")
            visible = bool((state or {}).get("visible"))
            if action:
                page.wait_for_timeout(100)
                self.log(
                    "已自动关闭隐私设置（"
                    + ("拒绝非必要 Cookie" if action == "reject" else "确认 Cookie 选择")
                    + "）"
                )
                return
            if visible:
                saw_dialog = True
            elif saw_dialog:
                self.log("隐私设置已自动关闭")
                return
            elif time.monotonic() < quiet_deadline:
                # 首页短暂等待弹窗；后续延迟出现由页面监视器处理。
                page.wait_for_timeout(100)
                continue
            else:
                return
            page.wait_for_timeout(100)
        if saw_dialog:
            raise AutomationError("隐私设置遮罩仍在阻挡页面操作")

    def _ensure_selected_option(
        self,
        page: Page,
        text: str,
        *,
        exact: bool,
        label: str,
    ) -> None:
        self._wait_for_page_ready(page, f"选择{label}前")
        option = self._visible_locator(
            page.get_by_text(text, exact=exact),
            label,
        )
        selected = option.evaluate(
            "element => Boolean(element.closest('.actived'))"
        )
        if not selected:
            option.click()
            self._wait_for_page_ready(page, f"更新{label}")
            selected = option.evaluate(
                "element => Boolean(element.closest('.actived'))"
            )
        if not selected:
            raise AutomationError(f"选项没有进入选中状态：{label}")
        self.log(f"已确认：{label}")

    def _fill_customer_info(
        self,
        page: Page,
        api: AdminApi,
        customer_id: int,
        email: str,
    ) -> None:
        self.stage("填写客户资料")
        defaults = self.config.registration
        contact_phone, chinese_address = registration_values_for_ordinal(
            defaults,
            self.batch_ordinal,
        )
        self._wait_for_page_ready(page, "客户资料表单")
        # 先选择寄送国家，官网才会按中国地址流程初始化后续表单。
        self._select_china(page)
        self._fill_placeholder_input(
            page,
            "请填写姓",
            defaults.last_name.strip(),
            "姓",
        )
        self._fill_placeholder_input(
            page,
            "请填写名",
            defaults.first_name.strip(),
            "名",
        )
        self._fill_placeholder_input(
            page,
            "请填写邮箱",
            email,
            "邮箱",
        )
        self._fill_placeholder_input(
            page,
            "请填写联系电话",
            contact_phone,
            "联系电话",
        )
        self._fill_placeholder_input(
            page,
            "请填写推荐人电话/推荐号码（选填）",
            (
                defaults.freecard_referrer.strip()
                if self.config.purchase_route == PURCHASE_ROUTE_FREECARD
                else defaults.referral_code.strip()
            ),
            (
                "推荐人号码"
                if self.config.purchase_route == PURCHASE_ROUTE_FREECARD
                else "推荐码"
            ),
        )

        code = self._obtain_verification_code(
            page,
            api,
            customer_id,
        )
        self._fill_placeholder_input(
            page,
            "请填写验证码",
            code,
            "邮箱验证码",
        )
        self.log("验证码已自动填入")

        address_suffix = defaults.address_suffix_start + self.batch_ordinal - 1
        self._smart_fill_address(
            page,
            defaults.chinese_address.strip(),
            address_suffix,
        )
        self.log(
            f"本单使用联系电话 {contact_phone}；实际提交地址："
            f"{chinese_address}"
        )
        self._ensure_marketing_off(page)
        self._click_button(page, "同意提交")
        if self.config.purchase_route == PURCHASE_ROUTE_FREECARD:
            self._confirm_freecard_address(page)
        else:
            page.wait_for_url(
                "**/buycard/buycardlist",
                timeout=self._automation_wait_timeout_ms(),
            )
            self._wait_for_page_ready(page, "订单确认页")

    def _confirm_freecard_address(self, page: Page) -> None:
        dialog = self._visible_locator(
            page.get_by_role("dialog"),
            "确认地址弹窗",
        )
        heading = dialog.get_by_role(
            "heading",
            name="确认地址",
            exact=True,
        )
        heading.wait_for(state="visible")
        confirm = self._visible_locator(
            dialog.get_by_text("确认支付", exact=True),
            "确认地址弹窗中的确认支付",
        )
        confirm.click()
        page.wait_for_url(
            "**/freecard/activityPageconfirm",
            timeout=self._automation_wait_timeout_ms(),
        )
        self._wait_for_page_ready(page, "£1 订单确认页")

    def _fill_placeholder_input(
        self,
        page: Page,
        placeholder: str,
        value: str,
        label: str,
    ) -> Locator:
        """网页会把 placeholder 同时放在表单外层和 input，只选择真实输入框。"""
        escaped = placeholder.replace("\\", "\\\\").replace('"', '\\"')
        field = self._visible_locator(
            page.locator(f'input[placeholder="{escaped}"]'),
            f"{label}输入框",
        )
        field.fill(value)
        return field

    def _visible_verification_cooldown(self, page: Page) -> str:
        """Inspect short-lived Element Plus alerts after requesting a code."""
        deadline = time.monotonic() + VERIFICATION_FEEDBACK_TIMEOUT_SECONDS
        saw_success = False
        while time.monotonic() < deadline:
            self._check_stop()
            text = ""
            with contextlib.suppress(Exception):
                text = str(
                    page.evaluate(
                        """() => Array.from(document.querySelectorAll(
                          '.el-message, .el-notification, [role="alert"]'
                        )).filter(node => {
                          const style = getComputedStyle(node);
                          const rect = node.getBoundingClientRect();
                          return style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && rect.width > 0
                            && rect.height > 0;
                        }).map(node => node.innerText || node.textContent || '')
                          .join(' | ')"""
                    )
                    or ""
                )
            notice = verification_cooldown_message(text)
            if notice:
                return notice
            if any(
                marker in text
                for marker in (
                    "验证码发送成功",
                    "验证码已发送",
                    "邮件发送成功",
                )
            ):
                # A previous Element-Plus success toast can still be visible
                # when the new cooldown response arrives.  Keep observing until
                # the success toast disappears so it cannot mask that response.
                saw_success = True
            elif saw_success:
                return ""
            self._wait_interruptibly(0.1)
        return ""

    def _cache_verification_code(
        self,
        customer_id: int,
        code: str,
    ) -> None:
        self.cached_verification_customer_id = int(customer_id)
        self.cached_verification_code = str(code)
        self.cached_verification_at = time.monotonic()

    def _clear_verification_code_cache(self) -> None:
        self.cached_verification_customer_id = None
        self.cached_verification_code = ""
        self.cached_verification_at = 0.0

    def _latest_recent_verification_code(
        self,
        api: AdminApi,
        customer_id: int,
        *,
        wait_seconds: float = 6.0,
    ) -> str:
        """Poll briefly for the newest still-valid code during website cooldown."""
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        last_error = ""
        while True:
            self._check_stop()
            try:
                latest = api.verification_code(customer_id)
            except ApiError as exc:
                last_error = str(exc)
            else:
                code = recent_verification_code(latest)
                if code:
                    return code
            if time.monotonic() >= deadline:
                break
            self._wait_interruptibly(min(1.0, deadline - time.monotonic()))
        detail = f"：{last_error}" if last_error else ""
        raise AutomationError(
            "验证码处于 180 秒冷却期，但邮箱中没有近期可核验验证码"
            + detail
        )

    def _obtain_verification_code(
        self,
        page: Page,
        api: AdminApi,
        customer_id: int,
    ) -> str:
        cache_age = time.monotonic() - self.cached_verification_at
        if (
            self.cached_verification_customer_id == int(customer_id)
            and re.fullmatch(r"\d{6}", self.cached_verification_code)
            and 0 <= cache_age < VERIFICATION_CODE_CACHE_SECONDS
        ):
            try:
                latest = api.verification_code(customer_id)
            except ApiError as exc:
                self.log(f"浏览器重试时验证码缓存复核失败：{exc}")
            else:
                latest_code = recent_verification_code(latest)
                if latest_code == self.cached_verification_code:
                    self.log(
                        "浏览器重试已从邮箱复核本单验证码，"
                        "跳过 180 秒内的重复发送"
                    )
                    return self.cached_verification_code
            self.log("验证码缓存已失效，重新检查网站发送状态")
            self._clear_verification_code_cache()

        baseline: dict[str, Any] = {}
        try:
            baseline = api.verification_code(customer_id)
        except ApiError as exc:
            self.log(f"验证码发送前邮箱基线读取暂未完成：{exc}")
        baseline_message_id = str(
            baseline.get("message_id") or ""
        ).strip()
        baseline_received_at = parse_message_timestamp(
            baseline.get("received_at")
        )
        if baseline_message_id:
            baseline_text = f"邮件 {baseline_message_id}"
            if baseline_received_at:
                baseline_text += (
                    f"，{self._format_mail_time(baseline_received_at)}"
                )
            self.log(f"已记录验证码发送前基线：{baseline_text}")

        requested_at = datetime.now(timezone.utc)
        self._click_visible_text(page, "获取验证码")
        cooldown = self._visible_verification_cooldown(page)
        if cooldown:
            self.log(
                f"检测到网站验证码冷却提示：{cooldown}；"
                "重新查询邮箱中的最新近期验证码"
            )
            code = self._latest_recent_verification_code(
                api,
                customer_id,
            )
            self._cache_verification_code(customer_id, code)
            self.log("冷却期近期验证码已复核并复用")
            return code

        self.log(
            "验证码已请求，等待新邮件；请求时间："
            f"{self._format_mail_time(requested_at)}"
        )
        self._wait_interruptibly(
            max(3, int(self.config.verification_min_wait_seconds))
        )
        code = self._poll_verification_code(
            api,
            customer_id,
            baseline_message_id=baseline_message_id,
            requested_at=requested_at,
        )
        self._cache_verification_code(customer_id, code)
        return code

    def _poll_verification_code(
        self,
        api: AdminApi,
        customer_id: int,
        *,
        baseline_message_id: str,
        requested_at: datetime,
    ) -> str:
        deadline = time.monotonic() + max(
            30,
            int(self.config.verification_timeout_seconds),
        )
        last_detail = ""
        last_rejection = ""
        while time.monotonic() < deadline:
            self._check_stop()
            try:
                result = api.verification_code(customer_id)
            except ApiError as exc:
                last_detail = str(exc)
            else:
                last_detail = str(result.get("detail") or "")
                code = str(result.get("code") or "").strip()
                if result.get("found") and re.fullmatch(r"\d{6}", code):
                    fresh, reason, received_at = assess_verification_freshness(
                        result,
                        baseline_message_id=baseline_message_id,
                        requested_at=requested_at,
                    )
                    if fresh:
                        timing = (
                            self._format_mail_time(received_at)
                            if received_at
                            else "新邮件 ID 已确认"
                        )
                        self.log(f"验证码邮件已核验：{timing}")
                        return code
                    marker = (
                        f"{result.get('message_id')}|"
                        f"{result.get('received_at')}|{reason}"
                    )
                    if marker != last_rejection:
                        last_rejection = marker
                        timing = (
                            f"，收件时间 {self._format_mail_time(received_at)}"
                            if received_at
                            else ""
                        )
                        self.log(f"已忽略旧验证码：{reason}{timing}")
            self._wait_interruptibly(3)
        raise AutomationError(
            "等待本次请求的新验证码超时"
            + (f"：{last_detail}" if last_detail else "")
        )

    def _wait_interruptibly(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0, float(seconds))
        while time.monotonic() < deadline:
            self._check_stop()
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))

    def _wait_for_page_ready(
        self,
        page: Page,
        label: str,
        *,
        stable_seconds: float = PAGE_READY_STABLE_SECONDS,
        stall_timeout_ms: Optional[int] = None,
    ) -> None:
        """等待 Loading 消失；DOM 有进度时重置默认 20 秒空闲计时。"""
        stall_ms = (
            AUTOMATION_STALL_TIMEOUT_MS
            if stall_timeout_ms is None
            else max(1_000, int(stall_timeout_ms))
        )
        stall_seconds = stall_ms / 1000
        total_seconds = max(
            stall_seconds,
            max(1_000, int(self.config.page_timeout_ms)) / 1000,
        )
        started = time.monotonic()
        last_progress_at = started
        last_fingerprint: Optional[tuple[Any, ...]] = None
        stable_since: Optional[float] = None
        saw_loading = False
        while time.monotonic() - started < total_seconds:
            self._check_stop()
            try:
                page_closed = page.is_closed()
            except Exception:
                page_closed = True
            if page_closed:
                raise RetryableStalledPageError(
                    f"页面已关闭，等待加载中止：{label}"
                )
            try:
                snapshot = self._page_progress_snapshot(page)
                loading = bool(snapshot.get("loading"))
            except Exception:
                if page.is_closed():
                    raise RetryableStalledPageError(
                        f"页面已关闭，等待加载中止：{label}"
                    )
                stable_since = None
                self._wait_interruptibly(0.25)
                continue
            now = time.monotonic()
            fingerprint = page_progress_fingerprint(snapshot)
            if last_fingerprint is None:
                last_fingerprint = fingerprint
            elif fingerprint != last_fingerprint:
                last_fingerprint = fingerprint
                last_progress_at = now
            if loading:
                if not saw_loading:
                    self.log(f"等待页面加载完成：{label}")
                    saw_loading = True
                stable_since = None
            else:
                if stable_since is None:
                    stable_since = now
                elif now - stable_since >= max(
                    0.25,
                    stable_seconds,
                ):
                    if saw_loading:
                        self.log(f"页面加载完成：{label}")
                    return
            if now - last_progress_at >= stall_seconds:
                if stall_ms == AUTOMATION_STALL_TIMEOUT_MS:
                    message = (
                        f"{label} 连续 20 秒没有 URL、Loading 或 DOM 变化"
                    )
                else:
                    message = (
                        f"{label} 连续 {int(stall_seconds)} 秒没有 URL、"
                        "Loading 或 DOM 变化"
                    )
                raise RetryableStalledPageError(
                    message
                )
            self._wait_interruptibly(0.1)
        raise RetryableStalledPageError(
            f"{label} 持续变化但超过 {int(total_seconds)} 秒仍未就绪"
        )

    @staticmethod
    def _format_mail_time(value: datetime) -> str:
        utc_value = value.astimezone(timezone.utc)
        beijing = utc_value.astimezone(timezone(timedelta(hours=8)))
        return (
            f"{beijing:%Y-%m-%d %H:%M:%S} 北京时间"
            f"（{utc_value:%H:%M:%S} UTC）"
        )

    def _select_china(self, page: Page) -> None:
        self._wait_for_page_ready(page, "选择寄送国家前")
        country = page.get_by_role("combobox", name=re.compile("寄送国家"))
        if country.count() != 1:
            raise AutomationError("寄送国家下拉框数量异常")
        # Element Plus 的透明 placeholder 覆盖 readonly input，点击 el-select 容器。
        select = country.locator(
            "xpath=ancestor::div["
            "contains(concat(' ', normalize-space(@class), ' '), ' el-select ')"
            "][1]"
        )
        if select.count() != 1:
            raise AutomationError("寄送国家下拉容器数量异常")
        select.click()
        option = page.get_by_role("option", name="中国", exact=True)
        option.wait_for(state="visible")
        option.click()
        self._wait_for_page_ready(page, "切换寄送国家")
        if "中国" not in select.inner_text():
            raise AutomationError("寄送国家没有成功切换为中国")
        self.log("寄送国家已选择中国")

    def _smart_fill_address(
        self,
        page: Page,
        base_address: str,
        suffix: int,
    ) -> None:
        expected_base = str(base_address or "").strip()
        if not expected_base:
            raise AutomationError("本单收货地址为空")
        expected_region = address_region_token(expected_base)
        region = page.get_by_role("textbox", name="*省市区", exact=True)
        detail = page.get_by_role("textbox", name="*详细地址", exact=True)
        with contextlib.suppress(Exception):
            detail.fill("")
        self._click_visible_text(page, "智能填写")
        dialog = page.get_by_role("dialog", name="智能填写")
        dialog.wait_for(state="visible")
        textboxes = dialog.get_by_role("textbox")
        if textboxes.count() != 1:
            raise AutomationError("智能填写弹窗的地址输入框数量异常")
        textboxes.fill("")
        textboxes.fill(expected_base)
        dialog_value = textboxes.input_value().strip()
        if dialog_value != expected_base:
            raise AutomationError(
                "智能填写弹窗没有写入本单设置地址："
                f"期望 {expected_base}，实际 {dialog_value or '空'}"
            )
        self.log(f"智能识别基础地址：{expected_base}")
        dialog.get_by_role("button", name="开始识别", exact=True).click()
        self._wait_for_page_ready(page, "智能识别地址")
        dialog.wait_for(state="hidden", timeout=self._automation_step_timeout_ms())

        region_value = region.input_value().strip()
        detail_value = detail.input_value().strip()
        if not region_value or not detail_value:
            raise AutomationError("智能填写没有生成完整的省市区和详细地址")
        if expected_region and expected_region not in normalize_address_text(
            region_value
        ):
            raise AutomationError(
                "智能填写仍保留了旧省市区："
                f"期望 {expected_region}，实际 {region_value}"
            )
        final_detail = append_address_suffix(detail_value, suffix)
        try:
            detail.fill("")
            detail.fill(final_detail)
            detail.press("Tab")
        except Exception as exc:
            raise AutomationError("详细地址尾号追加失败") from exc
        actual_detail = detail.input_value().strip()
        if actual_detail != final_detail:
            raise AutomationError(
                "详细地址尾号没有写入页面："
                f"期望 {final_detail}，实际 {actual_detail or '空'}"
            )
        self.log(
            f"地址识别完成并追加尾号 {suffix}："
            f"{region_value} / {actual_detail}"
        )

    def _ensure_marketing_off(self, page: Page) -> None:
        switches = page.locator('input[role="switch"]')
        for index in range(switches.count()):
            item = switches.nth(index)
            checked = item.is_checked()
            if checked:
                item.evaluate(
                    """input => {
                      const root = input.closest('.el-switch') || input.parentElement;
                      if (root) root.click();
                    }"""
                )
                self._wait_for_page_ready(page, "关闭营销订阅")

    def _confirm_order(self, page: Page) -> None:
        self.stage("确认订单")
        self._wait_for_page_ready(page, "订单确认页")
        defaults = self.config.registration
        if self.config.purchase_route == PURCHASE_ROUTE_50GB:
            coupon_code = defaults.coupon_code.strip()
            if not coupon_code:
                raise AutomationError("客户端设置中的优惠码为空")
            coupon = self._fill_placeholder_input(
                page,
                "请输入",
                coupon_code,
                "优惠码",
            )
            if coupon.input_value().strip() != coupon_code:
                raise AutomationError("优惠码没有完整写入结算页输入框")
            self.log(f"优惠码已填入并核对：{coupon_code}")
            self._click_button(page, "使用优惠码")
            expected = defaults.expected_price_gbp.strip()
            deadline = time.monotonic() + max(
                5,
                self._automation_step_timeout_ms() / 1000,
            )
            body_text = ""
            while time.monotonic() < deadline:
                self._check_stop()
                body_text = page.locator("body").inner_text()
                if price_is_expected(body_text, expected):
                    break
                rejection = coupon_rejection_message(body_text)
                if rejection:
                    raise AutomationError(
                        f"网站拒绝优惠码 {coupon_code}：{rejection}；"
                        "请在客户端设置中更换当前有效的优惠码"
                    )
                self._wait_interruptibly(0.25)
            else:
                raise RetryableStalledPageError(
                    f"优惠码 {coupon_code} 已提交，但订单金额没有变为 £{expected}"
                )
            if not price_is_expected(body_text, expected):
                raise AutomationError(
                    f"订单价格校验失败，预期 £{expected}"
                )
            self.log(f"优惠码已生效，最终价格 £{expected}")
        else:
            body_text = page.locator("body").inner_text()
            if not price_is_expected(body_text, "1.00"):
                raise AutomationError("£1 预存领卡订单金额校验失败")
            self.log("£1 预存领卡订单金额已核对")

        self._select_payment_method(page)

    def _select_payment_method(self, page: Page) -> None:
        """Select the configured CTExcel payment tile without re-submitting."""
        method = str(self.config.payment_method or PAYMENT_METHOD_WECHAT)
        method = method.strip().lower()
        if method not in {PAYMENT_METHOD_WECHAT, PAYMENT_METHOD_ALIPAY}:
            method = PAYMENT_METHOD_WECHAT

        other_payment = page.get_by_role(
            "radio",
            name="其他支付方式订购",
            exact=True,
        )
        if not other_payment.is_checked():
            other_payment.check()
            self._wait_for_page_ready(page, "切换其他支付方式")

        payment_tiles = page.locator(".pay-method-type > div")
        if method == PAYMENT_METHOD_WECHAT:
            label = "微信"
            named_tiles = payment_tiles.filter(has_text="微信")
            if not named_tiles.count():
                named_tiles = page.get_by_text("微信", exact=True)
        else:
            label = "支付宝"
            named_tiles = payment_tiles.filter(has_text="支付宝")
            if not named_tiles.count():
                named_tiles = page.get_by_text("支付宝", exact=True)

        tile: Optional[Locator] = None
        if named_tiles.count():
            with contextlib.suppress(RetryableStalledPageError):
                tile = self._visible_locator(
                    named_tiles,
                    f"{label}支付方式",
                )

        if tile is None and method == PAYMENT_METHOD_ALIPAY:
            # Current CTExcel builds render the AliPay/card gateway as an
            # image-only tile (the image contains Mastercard/Visa/AliPay
            # logos), so there is no text locator to click.  Distinguish it
            # from PayPal/WeChat by requiring an image and no text.
            empty_image_tiles: list[Locator] = []
            for index in range(payment_tiles.count()):
                candidate = payment_tiles.nth(index)
                with contextlib.suppress(Exception):
                    if (
                        candidate.is_visible()
                        and candidate.inner_text().strip() == ""
                        and candidate.locator("img").count() > 0
                    ):
                        empty_image_tiles.append(candidate)
            if empty_image_tiles:
                tile = empty_image_tiles[0]
                self.log(
                    "页面未提供独立支付宝文字按钮，已选择银行卡/支付宝支付网关"
                )

        if tile is None:
            raise RetryableStalledPageError(
                f"没有找到可见控件：{label}支付方式"
            )
        tile.click()
        self._wait_for_page_ready(page, f"切换{label}支付")
        selected = tile.evaluate(
            """el => Boolean(
              el.classList.contains('actived')
              || el.closest('.actived')
              || el.parentElement?.classList.contains('actived')
            )"""
        )
        if not selected:
            raise AutomationError(f"{label}支付方式没有进入选中状态")
        self.log(f"支付方式已切换为{label}")

    def _open_payment(
        self,
        page: Page,
        *,
        api: AdminApi,
        customer_id: int,
    ) -> tuple[Page, dict[str, str]]:
        if payment_method_is_alipay(self.config.payment_method):
            return self._open_alipay_payment(
                page,
                api=api,
                customer_id=customer_id,
            )
        return self._open_wechat_payment(
            page,
            api=api,
            customer_id=customer_id,
        )

    def _open_wechat_payment(
        self,
        page: Page,
        *,
        api: AdminApi,
        customer_id: int,
    ) -> tuple[Page, dict[str, str]]:
        self.stage("确认支付条款")
        self._click_button(page, "确认支付")
        dialogs = page.get_by_role("dialog")
        dialog = self._visible_locator(dialogs, "支付条款弹窗")
        self.log("支付条款弹窗已打开")
        self._submit_payment_terms(page, dialog)
        payment_page = self._wait_for_wechat_payment_page(page)
        self._wait_for_payment_page_content(payment_page)
        self._wait_for_page_ready(payment_page, "微信支付页")
        page_text = payment_page.locator("body").inner_text()
        order = ORDER_PATTERN.search(page_text)
        expected = (
            "1.00"
            if self.config.purchase_route == PURCHASE_ROUTE_FREECARD
            else self.config.registration.expected_price_gbp.strip()
        )
        if not payment_page_has_expected_amount(page_text, expected):
            raise AutomationError("微信支付页的英镑金额与所选申请路线不一致")
        order_number = order.group(0).upper() if order else ""
        if not order_number:
            raise AutomationError("微信支付页没有识别到 CTExcel 订单号")
        api.save_payment_checkpoint(
            customer_id,
            order_number=order_number,
            transaction_amount=expected,
        )
        self.log(
            "订单号和付款金额已回写客户管理"
            f"：{order_number} / £{expected}"
        )
        self.payment_qr_reached = True
        self.stage("等待人工支付")
        self.log(
            f"微信二维码已显示，金额 £{expected}"
            + (f"，订单号 {order_number}" if order_number else "")
        )
        return payment_page, {
            "order_number": order_number,
            "transaction_amount": expected,
            "payment_method": PAYMENT_METHOD_WECHAT,
        }

    def _open_alipay_payment(
        self,
        page: Page,
        *,
        api: AdminApi,
        customer_id: int,
    ) -> tuple[Page, dict[str, str]]:
        """Open CTExcel's AliPay/card gateway and wait for manual payment."""
        self.stage("确认支付条款")
        self._click_button(page, "确认支付")
        dialogs = page.get_by_role("dialog")
        dialog = self._visible_locator(dialogs, "支付条款弹窗")
        self.log("支付条款弹窗已打开")
        self._submit_payment_terms(page, dialog)
        payment_page = self._wait_for_alipay_payment_page(page)
        expected = (
            "1.00"
            if self.config.purchase_route == PURCHASE_ROUTE_FREECARD
            else self.config.registration.expected_price_gbp.strip()
        )
        payment_page = self._complete_alipay_gateway_selection(
            payment_page,
            expected_amount=expected,
        )
        order_number = self._page_order_number(payment_page)
        page_text = ""
        with contextlib.suppress(Exception):
            page_text = payment_page.locator("body").inner_text(timeout=1000)
        # Hosted gateways do not always repeat the CTExcel order amount.  If
        # they do show a recognizable amount, still reject a mismatched one.
        if re.search(r"(?:£|GBP)\s*[0-9]", page_text, re.I):
            if not payment_page_has_expected_amount(page_text, expected):
                raise AutomationError(
                    "支付宝支付页的英镑金额与所选申请路线不一致"
                )
        api.save_payment_checkpoint(
            customer_id,
            order_number=order_number,
            transaction_amount=expected,
        )
        self.payment_qr_reached = True
        self.stage("等待人工支付")
        self.log(
            f"支付宝支付页已打开，金额 £{expected}；"
            "请在 CTExcel 支付窗口完成付款"
        )
        return payment_page, {
            "order_number": order_number,
            "transaction_amount": expected,
            "payment_method": PAYMENT_METHOD_ALIPAY,
        }

    def _complete_alipay_gateway_selection(
        self,
        gateway_page: Page,
        *,
        expected_amount: str,
    ) -> Page:
        """Select AliPay in the hosted gateway, then open its QR page.

        Citi's hosted checkout first shows card and AliPay tiles.  The AliPay
        QR page is only opened after the tile is selected and the enabled
        ``支付 £…`` button is clicked, so merely detecting the gateway URL is
        not sufficient.
        """
        host = (urlsplit(str(gateway_page.url or "")).hostname or "").lower()
        if host.endswith(".alipay.com") or host == "alipay.com":
            return gateway_page
        if self._page_has_alipay_qr(gateway_page):
            return gateway_page

        self._select_alipay_gateway_option(gateway_page)
        self._click_alipay_gateway_pay(
            gateway_page,
            expected_amount=expected_amount,
        )
        return self._wait_for_alipay_qr_page(gateway_page)

    def _select_alipay_gateway_option(self, page: Page) -> None:
        deadline = time.monotonic() + max(
            5,
            self._automation_step_timeout_ms() / 1000,
        )
        while time.monotonic() < deadline:
            self._check_stop()
            result: Any = None
            with contextlib.suppress(Exception):
                result = page.evaluate(ALIPAY_GATEWAY_SELECT_SCRIPT)
            if isinstance(result, dict) and result.get("found"):
                if result.get("clicked"):
                    self.log("支付宝支付方式已在支付网关中选中")
                else:
                    self.log("支付宝支付方式已处于选中状态")
                return
            self._wait_interruptibly(0.2)
        raise RetryableStalledPageError(
            "支付宝支付网关未找到可选择的支付宝支付方式"
        )

    def _click_alipay_gateway_pay(
        self,
        page: Page,
        *,
        expected_amount: str,
    ) -> None:
        deadline = time.monotonic() + max(
            5,
            self._automation_step_timeout_ms() / 1000,
        )
        while time.monotonic() < deadline:
            self._check_stop()
            result: Any = None
            with contextlib.suppress(Exception):
                result = page.evaluate(
                    ALIPAY_GATEWAY_PAY_SCRIPT,
                    expected_amount,
                )
            if isinstance(result, dict) and result.get("found"):
                if result.get("enabled"):
                    self.log(
                        "支付宝支付方式已确认，已点击支付按钮；"
                        "等待跳转支付宝二维码"
                    )
                    return
                self._wait_interruptibly(0.2)
                continue
            self._wait_interruptibly(0.2)
        raise RetryableStalledPageError(
            "支付宝支付网关中的支付按钮未出现或仍处于禁用状态"
        )

    def _page_has_alipay_qr(self, page: Page) -> bool:
        with contextlib.suppress(Exception):
            return bool(page.evaluate(ALIPAY_QR_READY_SCRIPT))
        return False

    def _wait_for_alipay_qr_page(self, source_page: Page) -> Page:
        """Wait for the post-selection AliPay QR page or popup."""
        timeout_ms = max(
            PAYMENT_PAGE_STALL_TIMEOUT_MS,
            self._automation_wait_timeout_ms(),
        )
        deadline = time.monotonic() + timeout_ms / 1000
        initial_url = str(source_page.url or "")
        while time.monotonic() < deadline:
            self._check_stop()
            candidates: list[Page] = [source_page]
            if self.context is not None:
                with contextlib.suppress(Exception):
                    for candidate in self.context.pages:
                        if all(candidate is not item for item in candidates):
                            candidates.append(candidate)
            for candidate in reversed(candidates):
                try:
                    if candidate.is_closed():
                        continue
                    current_url = str(candidate.url or "")
                    if (
                        candidate is not source_page
                        and current_url == "about:blank"
                    ):
                        continue
                    if current_url and current_url != "about:blank":
                        if not is_payment_gateway_url(current_url):
                            self.log(
                                "已忽略非受信任域名的支付宝二维码窗口："
                                f"{urlsplit(current_url).hostname or '未知'}"
                            )
                            continue
                    if is_ctexcel_url(current_url) and is_payment_success_url(
                        current_url
                    ):
                        return candidate
                    host = (
                        urlsplit(current_url).hostname or ""
                    ).lower()
                    if host == "alipay.com" or host.endswith(".alipay.com"):
                        self.log("已跳转到支付宝二维码页面")
                        return candidate
                    if self._page_has_alipay_qr(candidate):
                        self.log("支付宝二维码已在当前支付窗口显示")
                        return candidate
                    if current_url != initial_url:
                        self.log("支付宝支付窗口已更新，等待二维码内容")
                except Exception:
                    continue
            self._wait_interruptibly(0.2)
        raise RetryableStalledPageError(
            "点击支付宝支付按钮后超过 45 秒，未进入支付宝二维码页面"
        )

    def _sync_payment_terms_checkbox(self, checkbox: Locator) -> None:
        """Wait until Element Plus and Vue both observe the checked value."""
        checked = checkbox.evaluate(
            """input => {
              if (!input.checked) {
                const root = input.closest('.el-checkbox') || input.parentElement;
                if (root) root.click();
              }
              return Boolean(input.checked);
            }"""
        )
        if not checked or not checkbox.is_checked():
            raise AutomationError("支付条款没有成功勾选")
        bound = checkbox.evaluate(
            f"""input => new Promise(resolve => {{
              const started = performance.now();
              const root = input.closest('.el-checkbox');
              const tick = () => {{
                const visualChecked = !root
                  || root.classList.contains('is-checked');
                if (input.checked && visualChecked) {{
                  resolve(true);
                  return;
                }}
                if (performance.now() - started
                    >= {PAYMENT_TERMS_BIND_TIMEOUT_MS}) {{
                  resolve(false);
                  return;
                }}
                requestAnimationFrame(tick);
              }};
              requestAnimationFrame(tick);
            }})"""
        )
        if not bound:
            raise AutomationError("支付条款虽已勾选，但页面状态没有完成同步")

    def _submit_payment_terms(self, page: Page, dialog: Locator) -> None:
        checkbox = dialog.get_by_role("checkbox")
        if checkbox.count() != 1:
            raise AutomationError("支付条款复选框数量异常")
        self._sync_payment_terms_checkbox(checkbox)
        self.log("支付条款已勾选并完成页面状态同步")
        submit = self._visible_locator(
            dialog.get_by_role("button", name="下一步", exact=True),
            "支付条款弹窗中的下一步",
        )
        try:
            submit.click(
                no_wait_after=True,
                timeout=min(
                    PAGE_CLICK_TIMEOUT_MS,
                    self._automation_step_timeout_ms(),
                ),
            )
        except PlaywrightTimeoutError:
            self.log("支付条款“下一步”点击未及时返回，转入付款页检测")
        self.log(
            "支付条款“下一步”仅提交一次；"
            "等待付款页或新窗口，避免重复创建订单"
        )

    def _page_order_number(self, page: Page) -> str:
        """Extract an order number from a gateway URL or rendered page."""
        values: list[str] = []
        with contextlib.suppress(Exception):
            values.append(str(page.url or ""))
            query = parse_qs(urlsplit(str(page.url or "")).query)
            for key in ("orderNo", "orderNumber", "order", "transactionId"):
                values.extend(str(item) for item in query.get(key, []))
        with contextlib.suppress(Exception):
            values.append(page.locator("body").inner_text(timeout=1000))
        for value in values:
            match = ORDER_PATTERN.search(value)
            if match:
                return match.group(0).upper()
        return ""

    def _wait_for_alipay_payment_page(self, source_page: Page) -> Page:
        """Follow the hosted AliPay/card gateway opened by CTExcel."""
        timeout_ms = max(
            PAYMENT_PAGE_STALL_TIMEOUT_MS,
            self._automation_wait_timeout_ms(),
        )
        deadline = time.monotonic() + timeout_ms / 1000
        initial_url = str(source_page.url or "")
        initial_iframes = source_page.locator("iframe").count()
        while time.monotonic() < deadline:
            self._check_stop()
            candidates: list[Page] = [source_page]
            if self.context is not None:
                with contextlib.suppress(Exception):
                    for candidate in self.context.pages:
                        if all(candidate is not item for item in candidates):
                            candidates.append(candidate)
            for candidate in reversed(candidates):
                try:
                    if candidate.is_closed():
                        continue
                    current_url = str(candidate.url or "")
                    if (
                        candidate is not source_page
                        and current_url == "about:blank"
                    ):
                        # A blank popup is not yet a payment page.  Wait for
                        # it to navigate to a configured HTTPS gateway host
                        # before inspecting or accepting its contents.
                        continue
                    if current_url and current_url != "about:blank":
                        if not is_payment_gateway_url(current_url):
                            self.log(
                                "已忽略非受信任域名的支付宝支付窗口："
                                f"{urlsplit(current_url).hostname or '未知'}"
                            )
                            continue
                    if is_payment_success_url(current_url):
                        return candidate
                    if (
                        candidate is not source_page
                        and current_url != "about:blank"
                    ):
                        url_marker = current_url.lower()
                        if any(
                            marker in url_marker
                            for marker in (
                                "payment",
                                "checkout",
                                "citi",
                                "alipay",
                            )
                        ):
                            self.log(
                                "支付宝支付页在新窗口打开，已自动切换后续跟踪"
                            )
                            return candidate
                    if candidate is source_page:
                        if current_url != initial_url:
                            self.log("已进入支付宝支付页")
                            return candidate
                        if candidate.locator("iframe").count() > initial_iframes:
                            self.log("支付宝支付网关已在当前页面打开")
                            return candidate
                        gateway_markers = candidate.locator(
                            "[id*='checkout'], [class*='checkout'], "
                            "[id*='payment'], [class*='payment']"
                        )
                        if gateway_markers.count() > 0:
                            self.log("支付宝支付网关已在当前页面打开")
                            return candidate
                    text = candidate.locator("body").inner_text(timeout=500)
                    compact = re.sub(r"\s+", "", text).lower()
                    if candidate is not source_page and any(
                        marker in compact
                        for marker in (
                            "支付宝",
                            "alipay",
                            "信用卡",
                            "银行卡",
                            "creditcard",
                            "payment",
                        )
                    ):
                        self.log("已进入支付宝支付页")
                        return candidate
                except Exception:
                    continue
            self._wait_interruptibly(0.2)
        raise RetryableStalledPageError(
            "支付宝支付页打开超过 45 秒，未检测到支付窗口"
        )

    def _wait_for_payment_page_content(self, payment_page: Page) -> str:
        """Wait for the same readiness contract used to select a payment page."""
        deadline = time.monotonic() + max(
            5,
            self._automation_wait_timeout_ms() / 1000,
        )
        last_text = ""
        while time.monotonic() < deadline:
            self._check_stop()
            try:
                last_text = payment_page.locator("body").inner_text(
                    timeout=500
                )
            except Exception:
                last_text = ""
            if payment_page_content_is_ready(last_text):
                return last_text
            self._wait_interruptibly(0.1)
        raise RetryableStalledPageError(
            "微信支付页已打开，但未出现二维码提示和有效订单号"
        )

    def _wait_for_wechat_payment_page(self, source_page: Page) -> Page:
        """Follow a same-tab redirect or the new tab used by some site builds."""
        timeout_ms = min(
            BROWSER_STARTUP_TIMEOUT_MS,
            max(5_000, int(self.config.page_timeout_ms)),
        )
        deadline = time.monotonic() + timeout_ms / 1000
        saw_extra_page = False
        next_proxy_check = time.monotonic()
        while time.monotonic() < deadline:
            self._check_stop()
            candidates: list[Page] = [source_page]
            if self.context is not None:
                with contextlib.suppress(Exception):
                    for candidate in self.context.pages:
                        if all(candidate is not item for item in candidates):
                            candidates.append(candidate)
            if len(candidates) > 1:
                saw_extra_page = True
            open_pages: list[Page] = []
            for candidate in reversed(candidates):
                try:
                    if candidate.is_closed():
                        continue
                    open_pages.append(candidate)
                    if (
                        is_ctexcel_url(candidate.url)
                        and is_wechat_payment_url(
                            candidate.url,
                            self.config.purchase_route,
                        )
                    ):
                        if candidate is not source_page:
                            self.log(
                                "微信支付页在新窗口打开，"
                                "已自动切换后续跟踪"
                            )
                        else:
                            self.log("已进入微信支付页")
                        return candidate
                    page_text = candidate.locator("body").inner_text(
                        timeout=500
                    )
                    if payment_page_content_is_ready(page_text):
                        self.log(
                            "微信支付二维码已在当前页面渲染，"
                            "无需等待网址跳转"
                        )
                        return candidate
                except Exception:
                    continue
            now = time.monotonic()
            if now >= next_proxy_check:
                next_proxy_check = now + 1
                for candidate in open_pages:
                    reason = self._page_proxy_error_reason(candidate)
                    if reason:
                        raise RetryableProxyBrowserError(reason)
            self._wait_interruptibly(0.1)
        detail = (
            "检测到新窗口，但新窗口未进入支付页"
            if saw_extra_page
            else "确认页未跳转，也未打开支付新窗口"
        )
        raise RetryableStalledPageError(
            f"生成微信支付订单 20 秒无新动作：{detail}"
        )

    def _wait_for_payment_success(
        self,
        page: Page,
        *,
        api: AdminApi,
        customer_id: int,
        email: str,
        pending_order: dict[str, str],
    ) -> AutomationResult:
        deadline = time.monotonic() + max(
            120,
            int(self.config.payment_timeout_seconds),
        )
        timeout_reported = False
        while True:
            self._check_stop()
            success_page = page
            if self.context is not None:
                with contextlib.suppress(Exception):
                    for candidate in self.context.pages:
                        if (
                            not candidate.is_closed()
                            and is_ctexcel_url(candidate.url)
                            and is_payment_success_url(candidate.url)
                        ):
                            success_page = candidate
                            break
            success_url = ""
            with contextlib.suppress(Exception):
                success_url = str(success_page.url or "")
            if is_ctexcel_url(success_url) and is_payment_success_url(success_url):
                order_number = str(
                    pending_order.get("order_number") or ""
                ).strip()
                if not order_number:
                    order_number = self._page_order_number(success_page)
                transaction_amount = str(
                    pending_order.get("transaction_amount") or ""
                ).strip()
                api.save_payment_checkpoint(
                    customer_id,
                    order_number=order_number,
                    transaction_amount=transaction_amount,
                    payment_succeeded=True,
                )
                self._delete_payment_qr("支付成功")
                self.log(
                    "支付成功已确认："
                    f"{order_number} / £{transaction_amount}；"
                    "客户端不再读取手机号，本单立即完成"
                )
                return AutomationResult(
                    customer_id=customer_id,
                    email=email,
                    order_number=order_number,
                    phone_number="",
                    transaction_amount=transaction_amount,
                )
            if not timeout_reported and time.monotonic() >= deadline:
                timeout_reported = True
                self._delete_payment_qr("支付等待超时")
                self.stage("支付等待超时，二维码已删除")
                self.log(
                    "支付等待已达到配置时限；Telegram 支付二维码已删除，"
                    "浏览器页面继续保留，完成支付后再结束流程"
                )
            page.wait_for_timeout(1000)

    def _capture_payment_qr(self, page: Page) -> bytes:
        deadline = time.monotonic() + min(
            30,
            max(5, int(self.config.step_timeout_ms) / 1000),
        )
        while time.monotonic() < deadline:
            self._check_stop()
            candidates = page.locator("img, canvas, svg")
            best: Optional[Locator] = None
            best_score = 0.0
            for index in range(candidates.count()):
                candidate = candidates.nth(index)
                with contextlib.suppress(Exception):
                    if not candidate.is_visible():
                        continue
                    box = candidate.bounding_box()
                    if not box:
                        continue
                    width = float(box.get("width") or 0)
                    height = float(box.get("height") or 0)
                    if (
                        width < 120
                        or height < 120
                        or width > 900
                        or height > 900
                    ):
                        continue
                    ratio = width / height if height else 0
                    if not 0.72 <= ratio <= 1.38:
                        continue
                    marker = str(
                        candidate.evaluate(
                            """node => [
                              node.id || '',
                              node.className || '',
                              node.getAttribute?.('src') || '',
                              node.getAttribute?.('alt') || ''
                            ].join(' ').toLowerCase()"""
                        )
                        or ""
                    )
                    score = width * height
                    if any(
                        token in marker
                        for token in (
                            "qr",
                            "qrcode",
                            "wechat",
                            "weixin",
                            "wx",
                            "二维码",
                            "data:image",
                        )
                    ):
                        score += 1_000_000
                    if score > best_score:
                        best = candidate
                        best_score = score
            if best is not None:
                image = best.screenshot(type="png")
                if image:
                    return image
            self._wait_interruptibly(1)
        self.log(
            "付款页未识别到独立二维码元素，改为发送当前付款页截图"
        )
        return page.screenshot(type="png", full_page=False)

    def _push_payment_qr(
        self,
        page: Page,
        *,
        customer_id: int,
        email: str,
        pending_order: dict[str, str],
    ) -> None:
        if not self.config.telegram.enabled:
            return
        try:
            image = self._capture_payment_qr(page)
            order_number = str(
                pending_order.get("order_number") or ""
            ).strip()
            amount = str(
                pending_order.get("transaction_amount") or ""
            ).strip()
            caption = (
                f"CTExcel 微信付款 · 线程 {self.worker_slot}\n"
                f"客户：#{customer_id}\n"
                f"邮箱：{email}\n"
                f"订单：{order_number}\n"
                f"金额：£{amount}"
            )
            with TelegramNotifier(self.config.telegram) as notifier:
                response = notifier.send_payment_qr(
                    image,
                    caption=caption,
                )
            result = response.get("result") if isinstance(response, dict) else None
            message_id = result.get("message_id") if isinstance(result, dict) else None
            try:
                normalized_message_id = int(message_id)
            except (TypeError, ValueError):
                normalized_message_id = 0
            if normalized_message_id <= 0:
                raise TelegramError("Telegram 返回中没有有效的二维码消息 ID")
            self.payment_qr_message_id = normalized_message_id
            self.log(
                "微信付款二维码已通过直连发送到 Telegram："
                f"{order_number}"
            )
        except TelegramError as exc:
            self.log(f"Telegram 二维码推送失败：{exc}")
        except Exception as exc:
            self.log(
                "Telegram 二维码截图失败："
                f"{type(exc).__name__}: {exc}"
            )

    def _delete_payment_qr(self, reason: str) -> None:
        """Best-effort deletion of the Telegram QR message.

        The message ID is cleared before the network call so repeated cleanup
        paths (success, timeout, stop, and browser teardown) never issue a
        second delete request for the same message.
        """
        message_id = self.payment_qr_message_id
        self.payment_qr_message_id = None
        if message_id is None:
            return
        try:
            with TelegramNotifier(self.config.telegram) as notifier:
                notifier.delete_message(message_id)
            self.log(f"Telegram 支付二维码已删除（{reason}）")
        except TelegramError as exc:
            self.log(f"Telegram 支付二维码删除失败（{reason}）：{exc}")
        except Exception as exc:
            self.log(
                "Telegram 支付二维码删除异常（"
                f"{reason}）：{type(exc).__name__}: {exc}"
            )

    def _visible_locator(self, locator: Locator, label: str) -> Locator:
        deadline = time.monotonic() + max(
            1,
            self._automation_step_timeout_ms() / 1000,
        )
        last_count = 0
        while time.monotonic() < deadline:
            self._check_stop()
            last_count = locator.count()
            for index in range(last_count):
                candidate = locator.nth(index)
                if candidate.is_visible():
                    return candidate
            time.sleep(0.15)
        raise RetryableStalledPageError(
            f"没有找到可见控件：{label}（页面匹配 {last_count} 个）"
        )

    def _click_visible_text(self, page: Page, text: str) -> None:
        self._wait_for_page_ready(page, f"操作“{text}”前")
        locator = self._visible_locator(
            page.get_by_text(text, exact=True),
            text,
        )
        locator.click()

    def _click_button(self, page: Page, name: str) -> None:
        self._wait_for_page_ready(page, f"点击“{name}”前")
        locator = self._visible_locator(
            page.get_by_role("button", name=name, exact=True),
            f"按钮“{name}”",
        )
        locator.click(no_wait_after=True)

    def _click_button_and_wait_for_page(
        self,
        page: Page,
        name: str,
        *,
        label: str,
        expected_path: str,
        ready_script: str,
    ) -> None:
        """Decouple a SPA click from Playwright's navigation auto-wait."""
        self.log(f"准备点击“{name}”并进入{label}")
        self._wait_for_page_ready(page, f"点击“{name}”前")
        locator = self._visible_locator(
            page.get_by_role("button", name=name, exact=True),
            f"按钮“{name}”",
        )
        try:
            locator.click(
                no_wait_after=True,
                timeout=min(
                    PAGE_CLICK_TIMEOUT_MS,
                    self._automation_step_timeout_ms(),
                ),
            )
            self.log(f"“{name}”点击已提交")
        except PlaywrightTimeoutError:
            reached, reason = self._page_target_state(
                page,
                expected_path=expected_path,
                ready_script=ready_script,
            )
            if reached:
                self.log(
                    f"“{name}”点击等待虽未返回，但{label}{reason}"
                )
            else:
                self.log(
                    f"“{name}”点击 5 秒未返回，已转入页面进度检测"
                )

        def retry_click() -> None:
            locator.evaluate("element => element.click()")

        self._wait_for_page_transition(
            page,
            label=label,
            expected_path=expected_path,
            ready_script=ready_script,
            retry_action=retry_click,
        )


class CTExcelBatchAutomation:
    """按配置并发运行多单；每个浏览器独立等待人工支付。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        log: LogCallback,
        stage: StageCallback,
        customer_created: CustomerCallback,
        item_started: Callable[[int, int], None],
        item_completed: Callable[[AutomationResult, int, int], None],
        completed_before: int = 0,
        automation_factory: Callable[..., CTExcelAutomation] = (
            CTExcelAutomation
        ),
    ):
        self.config = config
        self.log = log
        self.stage = stage
        self.customer_created = customer_created
        self.item_started = item_started
        self.item_completed = item_completed
        self.completed_before = max(0, int(completed_before))
        self.automation_factory = automation_factory
        self.stop_event = threading.Event()
        self.session: Optional[CTExcelAutomation] = None
        self.sessions: dict[int, CTExcelAutomation] = {}
        self.sessions_lock = threading.Lock()
        self.proxy_pool = (
            ProxyPoolRotator(config.proxy)
            if config.proxy.mode.strip().lower() == "pool"
            else None
        )
        self.qg_proxy_lock = threading.Lock()
        self.qg_proxy_ips: set[str] = set()
        self.resume_customer_ids_by_ordinal: dict[int, int] = {}
        self.resume_assignment_supported = False
        self.legacy_api_serial_required = False
        self.completed_ordinals: set[int] = set()

    def _set_completed_ordinals(self, completed: int) -> None:
        self.completed_ordinals = set(range(1, max(0, int(completed)) + 1))

    def _mark_completed_ordinal(self, ordinal: int) -> int:
        self.completed_ordinals.add(max(1, int(ordinal)))
        completed = 0
        while completed + 1 in self.completed_ordinals:
            completed += 1
        return completed

    def _prepare_resume_customer_ids(
        self,
        *,
        first_ordinal: int,
        total: int,
    ) -> None:
        """Assign distinct unfinished customers before any proxy is acquired."""
        self.resume_customer_ids_by_ordinal = {}
        self.resume_assignment_supported = False
        self.legacy_api_serial_required = False
        if self.automation_factory is not CTExcelAutomation:
            return
        self.stage("优先整理未完成客户")
        with AdminApi(
            self.config.server_url,
            self.config.app_password,
            retry_callback=self.log,
            sleep=lambda seconds: self.stop_event.wait(seconds),
        ) as api:
            status = api.connect()
            api_version = int(status.get("api_version") or 0)
            if api_version < 8:
                self.legacy_api_serial_required = True
                self.log(
                    "客户管理 API 版本较旧；为防止并发客户端重复领取"
                    "同一客户，本轮改为单线程并使用服务端即时复核"
                )
                return
            self.resume_assignment_supported = True
            pending = api.pending_customers()
            needed = max(0, int(total) - int(first_ordinal) + 1)
            resumable: list[int] = []
            for customer in sorted(
                pending,
                key=lambda item: int(item.get("customer_id") or 0),
            ):
                if len(resumable) >= needed:
                    break
                customer_id = int(customer.get("customer_id") or 0)
                if not customer_id:
                    continue
                if (
                    str(
                        customer.get("registration_confirmed_at") or ""
                    ).strip()
                    or str(
                        customer.get("payment_succeeded_at") or ""
                    ).strip()
                ):
                    continue
                # 已有订单号的旧记录先扫描邮箱，避免把实际已完成
                # 但尚未同步的账号再次提交。
                if str(customer.get("order_number") or "").strip():
                    try:
                        refreshed = api.sync_order_info(customer_id)
                    except ApiError as exc:
                        self.log(
                            f"客户 #{customer_id} 邮件状态刷新暂未完成：{exc}"
                        )
                    else:
                        if refreshed.get("registration_confirmed"):
                            self.log(
                                f"客户 #{customer_id} 已确认注册成功，跳过复用"
                            )
                            continue
                resumable.append(customer_id)
            self.resume_customer_ids_by_ordinal = {
                ordinal: customer_id
                for ordinal, customer_id in zip(
                    range(first_ordinal, total + 1),
                    resumable,
                )
            }
        if resumable:
            self.log(
                f"已按创建顺序为前 {len(resumable)} 单分配不同的"
                "未成功付款客户；已分配线程不再新建档案，"
                "队列不足的剩余任务才按需新建"
            )
        else:
            self.log("没有可复用的未完成客户，后续按需新建")

    def _next_unique_qg_proxy(self) -> dict[str, str]:
        """Serialize extraction and never assign one QG IP to two browsers."""
        with self.qg_proxy_lock:
            for _attempt in range(6):
                proxy = resolve_proxy(self.config.proxy)
                parsed = urlsplit(str(proxy.get("server") or ""))
                ip = parsed.hostname or ""
                if ip and ip not in self.qg_proxy_ips:
                    self.qg_proxy_ips.add(ip)
                    return proxy
            raise ProxyError("青果连续返回重复 IP，已停止创建共用节点的浏览器")

    def stop(self) -> None:
        self.stop_event.set()
        with self.sessions_lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            session.stop()
        if self.session and self.session not in sessions:
            self.session.stop()

    def _run_item(
        self,
        *,
        ordinal: int,
        total: int,
        worker_slot: int,
        reuse_pending_customer: bool,
        resume_customer_id: Optional[int] = None,
        browser_start_barrier: Optional[threading.Barrier] = None,
    ) -> AutomationResult:
        prefix = (
            f"[线程 {worker_slot} · 第 {ordinal} 单] "
            if min(total, self.config.continuous_workers) > 1
            else ""
        )

        def item_log(message: str) -> None:
            self.log(prefix + message)

        def item_stage(stage: str) -> None:
            self.stage(
                f"线程 {worker_slot}：{stage}"
                if prefix
                else stage
            )

        def item_customer(payload: dict[str, Any]) -> None:
            self.customer_created(
                {
                    **payload,
                    "batch_ordinal": ordinal,
                    "worker_slot": worker_slot,
                }
            )

        proxy_override: Optional[dict[str, str]] = None
        proxy_provider: Optional[Callable[[], dict[str, str]]] = None
        if (
            self.config.proxy.mode.strip().lower() == "api"
            and is_qg_proxy_api_url(self.config.proxy.api_url)
        ):
            def provide_qg_proxy() -> dict[str, str]:
                proxy = self._next_unique_qg_proxy()
                item_log(
                    "客户邮箱已就绪，现在为本浏览器独立提取青果节点："
                    f"{masked_proxy_label(proxy)}"
                )
                return proxy

            proxy_provider = provide_qg_proxy
        elif self.proxy_pool is not None:
            lease = self.proxy_pool.next()
            proxy_override = lease.proxy
            item_log(
                "代理池分配："
                f"节点 {lease.pool_index}/{lease.pool_size}，"
                f"本节点第 {lease.use_number}/{lease.use_limit} 次使用，"
                f"{masked_proxy_label(lease.proxy)}"
            )

        session = self.automation_factory(
            self.config,
            log=item_log,
            stage=item_stage,
            customer_created=item_customer,
            request_key=(
                f"batch-{uuid.uuid4().hex}-{ordinal}"
            ),
            reuse_pending_customer=reuse_pending_customer,
            resume_customer_id=resume_customer_id,
            worker_slot=worker_slot,
            proxy_override=proxy_override,
            proxy_provider=proxy_provider,
            browser_start_barrier=browser_start_barrier,
            batch_ordinal=ordinal,
        )
        if self.stop_event.is_set():
            with contextlib.suppress(Exception):
                session.stop()
            raise AutomationError("用户已停止连续申请")
        with self.sessions_lock:
            if self.stop_event.is_set():
                with contextlib.suppress(Exception):
                    session.stop()
                raise AutomationError("用户已停止连续申请")
            self.sessions[worker_slot] = session
        if worker_slot == 1:
            self.session = session
        try:
            if self.stop_event.is_set():
                session.stop()
                raise AutomationError("用户已停止连续申请")
            result = session.run()
            result.batch_ordinal = ordinal
            result.worker_slot = worker_slot
            return result
        finally:
            with self.sessions_lock:
                self.sessions.pop(worker_slot, None)
            if self.session is session:
                self.session = None

    def _run_serial(
        self,
        *,
        total: int,
        completed: int,
    ) -> AutomationBatchResult:
        last_result: Optional[AutomationResult] = None
        for ordinal in range(completed + 1, total + 1):
            if self.stop_event.is_set():
                raise AutomationError("用户已停止连续申请")
            self.item_started(ordinal, total)
            self.log(f"开始第 {ordinal} / {total} 单申请")
            resume_customer_id = self.resume_customer_ids_by_ordinal.get(
                ordinal
            )
            last_result = self._run_item(
                ordinal=ordinal,
                total=total,
                worker_slot=1,
                reuse_pending_customer=(
                    resume_customer_id is not None
                    if self.resume_assignment_supported
                    else True
                ),
                resume_customer_id=resume_customer_id,
            )
            completed = self._mark_completed_ordinal(ordinal)
            self.item_completed(last_result, completed, total)
            if completed >= total:
                break
            self.log(
                f"第 {ordinal} 单支付完成；"
                f"{total - completed} 单等待继续"
            )
            delay = max(
                0,
                min(60, int(self.config.continuous_interval_seconds)),
            )
            if self.stop_event.wait(delay):
                raise AutomationError("用户已停止连续申请")
        return AutomationBatchResult(
            completed_count=completed,
            total_count=total,
            last_result=last_result,
        )

    def _run_parallel(
        self,
        *,
        total: int,
        completed: int,
        workers: int,
    ) -> AutomationBatchResult:
        last_result: Optional[AutomationResult] = None
        next_ordinal = completed + 1
        first_ordinal = next_ordinal
        first_wave_last_ordinal = min(
            total,
            first_ordinal + workers - 1,
        )
        browser_start_barrier = threading.Barrier(
            first_wave_last_ordinal - first_ordinal + 1
        )
        available_slots = list(range(1, workers + 1))
        futures: dict[Future[AutomationResult], tuple[int, int]] = {}

        def submit(
            executor: ThreadPoolExecutor,
            *,
            ordinal: int,
            worker_slot: int,
        ) -> None:
            self.item_started(ordinal, total)
            self.log(
                f"线程 {worker_slot} 开始第 {ordinal} / {total} 单申请"
            )
            future = executor.submit(
                self._run_item,
                ordinal=ordinal,
                total=total,
                worker_slot=worker_slot,
                reuse_pending_customer=(
                    ordinal in self.resume_customer_ids_by_ordinal
                    if self.resume_assignment_supported
                    else ordinal == first_ordinal
                ),
                resume_customer_id=(
                    self.resume_customer_ids_by_ordinal.get(ordinal)
                ),
                browser_start_barrier=(
                    browser_start_barrier
                    if ordinal <= first_wave_last_ordinal
                    else None
                ),
            )
            futures[future] = (ordinal, worker_slot)

        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="ctexcel",
        ) as executor:
            while next_ordinal <= total and available_slots:
                slot = available_slots.pop(0)
                submit(
                    executor,
                    ordinal=next_ordinal,
                    worker_slot=slot,
                )
                next_ordinal += 1

            while futures:
                if self.stop_event.is_set():
                    self.stop()
                    raise AutomationError("用户已停止连续申请")
                done, _pending = wait(
                    tuple(futures),
                    timeout=0.5,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    continue
                for future in done:
                    ordinal, slot = futures.pop(future)
                    try:
                        last_result = future.result()
                    except Exception:
                        self.stop()
                        for pending in futures:
                            pending.cancel()
                        raise
                    completed = self._mark_completed_ordinal(ordinal)
                    self.item_completed(
                        last_result,
                        completed,
                        total,
                    )
                    self.log(
                        f"线程 {slot} 完成第 {ordinal} 单；"
                        f"总进度 {completed} / {total}"
                    )
                    if next_ordinal <= total:
                        submit(
                            executor,
                            ordinal=next_ordinal,
                            worker_slot=slot,
                        )
                        next_ordinal += 1
                    else:
                        available_slots.append(slot)

        return AutomationBatchResult(
            completed_count=self._mark_completed_ordinal(total),
            total_count=total,
            last_result=last_result,
        )

    def run(self) -> AutomationBatchResult:
        total = application_target(self.config)
        # 启动前一次性校验完整区间，避免运行到中途才发现尾号不足。
        defaults = self.config.registration
        if defaults.contact_phone.strip() and defaults.chinese_address.strip():
            registration_values_for_ordinal(defaults, total)
        completed = min(self.completed_before, total)
        self._set_completed_ordinals(completed)
        if completed >= total:
            return AutomationBatchResult(
                completed_count=completed,
                total_count=total,
            )
        self._prepare_resume_customer_ids(
            first_ordinal=completed + 1,
            total=total,
        )
        workers = (
            min(
                total - completed,
                max(1, min(10, int(self.config.continuous_workers))),
            )
            if self.config.continuous_enabled
            else 1
        )
        if self.legacy_api_serial_required and workers > 1:
            self.log(
                f"已暂停配置的 {workers} 线程并发；"
                "服务端升级到 API v8 后会自动恢复并发"
            )
            workers = 1
        if workers <= 1:
            return self._run_serial(
                total=total,
                completed=completed,
            )
        self.log(
            f"并发调度已启动：{workers} 个独立浏览器线程"
        )
        return self._run_parallel(
            total=total,
            completed=completed,
            workers=workers,
        )
