from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import contextlib
from pathlib import Path
import re
import shutil
import threading
import tempfile
import time
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

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
    PURCHASE_ROUTE_50GB,
    PURCHASE_ROUTE_FREECARD,
    app_config_dir,
)
from .proxy import (
    ProxyError,
    masked_proxy_label,
    prepare_proxy,
)


LogCallback = Callable[[str], None]
StageCallback = Callable[[str], None]
CustomerCallback = Callable[[dict[str, Any]], None]

ORDER_PATTERN = re.compile(
    r"\b(?:ORDER\d{12,}|ORDERSUK\d{12,})\b",
    re.I,
)
LOADING_OVERLAY_SCRIPT = """() => {
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
  const selectors = [
    '.el-loading-mask',
    '.el-loading-spinner',
    '[class*="loading-mask"]',
    '[class*="loadingMask"]'
  ];
  return selectors.some(selector =>
    Array.from(document.querySelectorAll(selector)).some(visible)
  );
}"""
class AutomationError(RuntimeError):
    pass


@dataclass
class AutomationResult:
    customer_id: int
    email: str
    order_number: str = ""
    phone_number: str = ""
    transaction_amount: str = ""


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
    ):
        self.config = config
        self.log = log
        self.stage = stage
        self.customer_created = customer_created
        self.stop_event = threading.Event()
        self.context: Optional[BrowserContext] = None
        self.profile_dir: Optional[Path] = None
        self.network_events: list[str] = []

    def stop(self) -> None:
        self.stop_event.set()

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise AutomationError("用户已停止当前流程")

    def run(self) -> AutomationResult:
        self._validate_registration_defaults()
        self.stage("准备浏览器代理")
        try:
            prepared_proxy = prepare_proxy(self.config.proxy)
            browser_proxy = prepared_proxy.playwright_proxy
            if prepared_proxy.public_ip:
                self.log(
                    f"当前出口公网 IP：{prepared_proxy.public_ip}"
                )
            elif prepared_proxy.public_ip_error:
                self.log(prepared_proxy.public_ip_error)
            if browser_proxy:
                source = (
                    "动态提取"
                    if self.config.proxy.mode == "api"
                    else "固定配置"
                )
                self.log(
                    f"{source}代理已就绪：{masked_proxy_label(browser_proxy)}"
                )
            else:
                self.log("浏览器使用直连")
        except ProxyError as exc:
            raise AutomationError(f"浏览器代理准备失败：{exc}") from exc

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
            self._refresh_pending_customers(api)
            created = api.create_ctexcel_customer(
                allow_new_after_checkpoint=(
                    self.config.continuous_enabled
                ),
            )
            customer_id = int(created["customer_id"])
            email = str(created["email"])
            task = {
                "customer_id": customer_id,
                "email": email,
                "product_type": "ctexcel",
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
            return self._run_browser(
                api,
                customer_id,
                email,
                browser_proxy=browser_proxy,
            )

    def _refresh_pending_customers(self, api: AdminApi) -> None:
        """开始新流程前先扫描无手机号客户，避免重复建立空记录。"""
        pending = api.pending_customers()
        if not pending:
            self.log("没有无手机号的待完成客户，将新建客户")
            return
        confirmed_pending = [
            customer
            for customer in pending
            if str(
                customer.get("registration_confirmed_at") or ""
            ).strip()
        ]
        if confirmed_pending:
            self.log(
                f"{len(confirmed_pending)} 个账号已有订单确认邮件，"
                "不会再次提交注册"
            )
        scan_targets = [
            customer
            for customer in pending
            if not str(
                customer.get("registration_confirmed_at") or ""
            ).strip()
        ]
        if self.config.continuous_enabled:
            scan_targets = [
                customer
                for customer in scan_targets
                if not str(customer.get("order_number") or "").strip()
            ]
            paid_pending = len(pending) - len(
                scan_targets
            ) - len(confirmed_pending)
            if paid_pending:
                self.log(
                    f"{paid_pending} 个已生成订单的客户由服务器后台同步，"
                    "不阻塞本轮连续申请"
                )
        if not scan_targets:
            return
        self.log(
            f"检测到 {len(scan_targets)} 个未生成订单的中断客户，"
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
            missing.append("固定联系电话")
        if not defaults.chinese_address.strip():
            missing.append("固定中国收货地址")
        if missing:
            raise AutomationError("请先填写：" + "、".join(missing))
        if not re.fullmatch(r"1\d{10}", defaults.contact_phone.strip()):
            raise AutomationError("固定联系电话应为 11 位中国手机号码")
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
    ) -> AutomationResult:
        self.stage("启动浏览器")
        with sync_playwright() as playwright:
            profile_root = Path(app_config_dir()) / "browser-runs"
            profile_root.mkdir(parents=True, exist_ok=True)
            self.profile_dir = Path(
                tempfile.mkdtemp(
                    prefix="order-",
                    dir=str(profile_root),
                )
            )
            launch_options: dict[str, Any] = {
                "headless": bool(self.config.headless),
                # 该流程用于逐单人工支付；保留可观察的操作节奏，避免连续快速点击。
                "slow_mo": max(800, int(self.config.slow_mo_ms)),
                # 去掉 Chrome 的自动测试横幅和最明显的 webdriver 标记。
                "ignore_default_args": ["--enable-automation"],
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            }
            channel = (self.config.browser_channel or "").strip().lower()
            if channel and channel != "chromium":
                launch_options["channel"] = channel
            if browser_proxy:
                launch_options["proxy"] = browser_proxy
            self.context = playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                **launch_options,
            )
            self.context.add_init_script(
                """
                Object.defineProperty(
                  Navigator.prototype,
                  'webdriver',
                  {get: () => undefined, configurable: true}
                );
                """
            )
            self.log(
                "已启用浏览器兼容模式：每单使用独立临时配置，"
                "并移除 Chrome/Edge 的自动测试标记"
            )
            page: Optional[Page] = None
            try:
                page = self.context.pages[0] if self.context.pages else self.context.new_page()
                self._attach_page_diagnostics(page)
                page.set_default_timeout(max(1000, int(self.config.step_timeout_ms)))
                page.set_default_navigation_timeout(
                    max(5000, int(self.config.page_timeout_ms))
                )
                if self.config.purchase_route == PURCHASE_ROUTE_FREECARD:
                    self._start_freecard_application(page)
                else:
                    self._select_plan(page)
                    self._configure_sim(page)
                self._fill_customer_info(page, api, customer_id, email)
                self._confirm_order(page)
                pending = self._open_wechat_payment(
                    page,
                    api=api,
                    customer_id=customer_id,
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
                if page is not None:
                    self._preserve_error_page(page, exc)
                raise
            finally:
                with contextlib.suppress(Exception):
                    self.context.close()
                self.context = None
                if self.profile_dir:
                    with contextlib.suppress(Exception):
                        shutil.rmtree(self.profile_dir)
                self.profile_dir = None

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
                if status >= 400:
                    self._record_network_event(
                        f"HTTP {status} "
                        f"{getattr(request, 'method', 'GET')} "
                        f"{parsed.path}"
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
        stamp = time.strftime("%Y%m%d-%H%M%S")
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
        page.goto(
            FREECARD_APPLICATION_URL,
            wait_until="domcontentloaded",
            timeout=self.config.page_timeout_ms,
        )
        self._dismiss_cookie_consent(page)
        self._wait_for_page_ready(page, "£1 领卡活动页")
        page.wait_for_function(
            """() => {
              const text = document.body?.innerText || '';
              return text.includes('还没选好套餐')
                && text.includes('先预存£1领卡');
            }""",
            timeout=self.config.page_timeout_ms,
        )
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
            timeout=self.config.page_timeout_ms,
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
        self._click_button(page, "下一步")
        page.wait_for_url(
            "**/freecard/activityPagefillInfos",
            timeout=self.config.page_timeout_ms,
        )
        self._wait_for_page_ready(page, "£1 领卡资料页")

    def _select_plan(self, page: Page) -> None:
        self.stage("选择申请路线")
        page.goto(
            self.config.application_url,
            wait_until="domcontentloaded",
            timeout=self.config.page_timeout_ms,
        )
        self._dismiss_cookie_consent(page)
        self._wait_for_page_ready(page, "套餐列表")
        page.wait_for_function(
            "() => document.body && document.body.innerText.includes('50GB')",
            timeout=self.config.page_timeout_ms,
        )
        selected = page.evaluate(
            """() => {
              const norm = value => String(value || '').replace(/\\s+/g, '');
              const nodes = Array.from(document.querySelectorAll('*'));
              const anchors = nodes.filter(el =>
                el.children.length === 0 && norm(el.textContent) === '50GB'
              );
              for (const anchor of anchors) {
                let card = anchor;
                for (let depth = 0; depth < 9 && card; depth += 1, card = card.parentElement) {
                  const text = norm(card.innerText);
                  if (!text.includes('£11.9/30天') || !text.includes('立即订购')) continue;
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
        )
        if not selected:
            raise AutomationError("没有定位到 50GB / £11.9 套餐的立即订购按钮")
        page.wait_for_url(
            "**/buycard/simcarddetails/**",
            timeout=self.config.page_timeout_ms,
        )
        self._wait_for_page_ready(page, "套餐详情")
        self.log("已选择 50GB、£11.9/30天套餐")

    def _configure_sim(self, page: Page) -> None:
        self.stage("配置 SIM / 套餐")
        self._dismiss_cookie_consent(page)
        self._wait_for_page_ready(page, "SIM 卡配置页")
        page.wait_for_function(
            """() => {
              const text = document.body?.innerText || '';
              return text.includes('SIM卡类型') && text.includes('自动续订');
            }""",
            timeout=self.config.page_timeout_ms,
        )
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
                timeout=self.config.step_timeout_ms,
            )
        switch_class = switch.get_attribute("class") or ""
        if "is-checked" in switch_class:
            raise AutomationError("自动续订仍处于开启状态")
        self.log("实体 SIM、免费随机号码、1个月、1张，自动续订已关闭")
        self._click_button(page, "下一步")
        page.wait_for_url("**/buycard/fillinfos", timeout=self.config.page_timeout_ms)
        self._wait_for_page_ready(page, "客户资料页")

    def _dismiss_cookie_consent(self, page: Page) -> None:
        """拒绝非必要 Cookie，并移除会拦截页面点击的 Usercentrics 遮罩。"""
        host = page.locator("#usercentrics-cmp-ui")
        try:
            host.wait_for(state="attached", timeout=5000)
        except PlaywrightTimeoutError:
            return
        deny = page.locator(
            "#usercentrics-cmp-ui button.uc-deny-button"
        )
        try:
            if deny.count() and deny.first.is_visible():
                deny.first.click(timeout=self.config.step_timeout_ms)
                page.wait_for_function(
                    """() => {
                      const host = document.querySelector('#usercentrics-cmp-ui');
                      const overlay = host?.shadowRoot?.querySelector('.overlay');
                      return !overlay || !(overlay.offsetWidth || overlay.offsetHeight);
                    }""",
                    timeout=self.config.step_timeout_ms,
                )
                self.log("已关闭隐私设置遮罩")
        except PlaywrightTimeoutError as exc:
            raise AutomationError("隐私设置遮罩仍在阻挡页面操作") from exc

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
            defaults.contact_phone.strip(),
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

        baseline: dict[str, Any] = {}
        try:
            baseline = api.verification_code(customer_id)
        except ApiError as exc:
            self.log(f"验证码发送前邮箱基线读取暂未完成：{exc}")
        baseline_message_id = str(baseline.get("message_id") or "").strip()
        baseline_received_at = parse_message_timestamp(baseline.get("received_at"))
        if baseline_message_id:
            baseline_text = f"邮件 {baseline_message_id}"
            if baseline_received_at:
                baseline_text += f"，{self._format_mail_time(baseline_received_at)}"
            self.log(f"已记录验证码发送前基线：{baseline_text}")

        requested_at = datetime.now(timezone.utc)
        self._click_visible_text(page, "获取验证码")
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
        self._fill_placeholder_input(
            page,
            "请填写验证码",
            code,
            "邮箱验证码",
        )
        self.log("验证码已自动填入")

        self._smart_fill_address(page, defaults.chinese_address.strip())
        self._ensure_marketing_off(page)
        self._click_button(page, "同意提交")
        if self.config.purchase_route == PURCHASE_ROUTE_FREECARD:
            self._confirm_freecard_address(page)
        else:
            page.wait_for_url(
                "**/buycard/buycardlist",
                timeout=self.config.page_timeout_ms,
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
            timeout=self.config.page_timeout_ms,
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
        stable_seconds: float = 1.2,
    ) -> None:
        """等待全屏 Loading 消失并保持稳定，避免请求刚结束就继续点击。"""
        configured_timeout = max(
            90_000,
            int(self.config.step_timeout_ms) * 3,
        )
        timeout_ms = min(
            max(5_000, int(self.config.page_timeout_ms)),
            configured_timeout,
        )
        deadline = time.monotonic() + timeout_ms / 1000
        stable_since: Optional[float] = None
        saw_loading = False
        while time.monotonic() < deadline:
            self._check_stop()
            try:
                loading = bool(page.evaluate(LOADING_OVERLAY_SCRIPT))
            except Exception:
                if page.is_closed():
                    raise AutomationError(
                        f"页面已关闭，等待加载中止：{label}"
                    )
                stable_since = None
                self._wait_interruptibly(0.25)
                continue
            if loading:
                if not saw_loading:
                    self.log(f"等待页面加载完成：{label}")
                    saw_loading = True
                stable_since = None
            else:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= max(
                    0.5,
                    stable_seconds,
                ):
                    if saw_loading:
                        with contextlib.suppress(PlaywrightTimeoutError):
                            page.wait_for_load_state(
                                "networkidle",
                                timeout=3000,
                            )
                        self.log(f"页面加载完成：{label}")
                    return
            self._wait_interruptibly(0.2)
        raise AutomationError(
            f"页面加载超时：{label}；Loading 遮罩持续未消失"
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

    def _smart_fill_address(self, page: Page, address: str) -> None:
        self._click_visible_text(page, "智能填写")
        dialog = page.get_by_role("dialog", name="智能填写")
        dialog.wait_for(state="visible")
        textboxes = dialog.get_by_role("textbox")
        if textboxes.count() != 1:
            raise AutomationError("智能填写弹窗的地址输入框数量异常")
        textboxes.fill(address)
        dialog.get_by_role("button", name="开始识别", exact=True).click()
        self._wait_for_page_ready(page, "智能识别地址")
        dialog.wait_for(state="hidden", timeout=self.config.step_timeout_ms)

        region = page.get_by_role("textbox", name="*省市区", exact=True)
        detail = page.get_by_role("textbox", name="*详细地址", exact=True)
        region_value = region.input_value().strip()
        detail_value = detail.input_value().strip()
        if not region_value or not detail_value:
            raise AutomationError("智能填写没有生成完整的省市区和详细地址")
        self.log(f"地址识别完成：{region_value} / {detail_value}")

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
            self._wait_interruptibly(2)
            self._click_button(page, "使用优惠码")
            expected = defaults.expected_price_gbp.strip()
            deadline = time.monotonic() + max(
                5,
                int(self.config.step_timeout_ms) / 1000,
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
                raise AutomationError(
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

        other_payment = page.get_by_role(
            "radio",
            name="其他支付方式订购",
            exact=True,
        )
        if not other_payment.is_checked():
            other_payment.check()
            self._wait_for_page_ready(page, "切换其他支付方式")
        wechat = self._visible_locator(
            page.get_by_text("微信", exact=True),
            "微信支付方式",
        )
        wechat.click()
        self._wait_for_page_ready(page, "切换微信支付")
        selected = wechat.evaluate(
            """el => Boolean(
              el.closest('.actived')
              || el.parentElement?.classList.contains('actived')
            )"""
        )
        if not selected:
            raise AutomationError("微信支付方式没有进入选中状态")
        self.log("支付方式已切换为微信")

    def _open_wechat_payment(
        self,
        page: Page,
        *,
        api: AdminApi,
        customer_id: int,
    ) -> dict[str, str]:
        self.stage("确认支付条款")
        self._click_button(page, "确认支付")
        dialogs = page.get_by_role("dialog")
        dialog = self._visible_locator(dialogs, "支付条款弹窗")
        checkbox = dialog.get_by_role("checkbox")
        if checkbox.count() != 1:
            raise AutomationError("支付条款复选框数量异常")
        checkbox.evaluate(
            """input => {
              if (!input.checked) {
                const root = input.closest('.el-checkbox') || input.parentElement;
                if (root) root.click();
              }
            }"""
        )
        if not checkbox.is_checked():
            raise AutomationError("支付条款没有成功勾选")
        dialog.get_by_role("button", name="下一步", exact=True).click()
        self._wait_for_page_ready(page, "生成微信支付订单")
        payment_path = (
            "**/freecard/buycardWX"
            if self.config.purchase_route == PURCHASE_ROUTE_FREECARD
            else "**/buycard/buycardWX"
        )
        page.wait_for_url(payment_path, timeout=self.config.page_timeout_ms)
        page.wait_for_function(
            "() => document.body && document.body.innerText.includes('订单号码')",
            timeout=self.config.page_timeout_ms,
        )
        self._wait_for_page_ready(page, "微信支付页")
        page_text = page.locator("body").inner_text()
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
        self.stage("等待人工微信支付")
        self.log(
            f"微信二维码已显示，金额 £{expected}"
            + (f"，订单号 {order_number}" if order_number else "")
        )
        return {
            "order_number": order_number,
            "transaction_amount": expected,
        }

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
        while time.monotonic() < deadline:
            self._check_stop()
            if is_payment_success_url(page.url):
                order_number = str(
                    pending_order.get("order_number") or ""
                ).strip()
                transaction_amount = str(
                    pending_order.get("transaction_amount") or ""
                ).strip()
                api.save_payment_checkpoint(
                    customer_id,
                    order_number=order_number,
                    transaction_amount=transaction_amount,
                )
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
            page.wait_for_timeout(1000)
        raise AutomationError("等待人工支付完成超时，可在客户端重新载入该客户继续")

    def _visible_locator(self, locator: Locator, label: str) -> Locator:
        deadline = time.monotonic() + max(
            1,
            int(self.config.step_timeout_ms) / 1000,
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
        raise AutomationError(
            f"没有找到可见控件：{label}（页面匹配 {last_count} 个）"
        )

    def _click_visible_text(self, page: Page, text: str) -> None:
        self._wait_for_page_ready(page, f"操作“{text}”前")
        locator = self._visible_locator(
            page.get_by_text(text, exact=True),
            text,
        )
        locator.click()
        self._wait_for_page_ready(page, f"操作“{text}”")

    def _click_button(self, page: Page, name: str) -> None:
        self._wait_for_page_ready(page, f"点击“{name}”前")
        locator = self._visible_locator(
            page.get_by_role("button", name=name, exact=True),
            f"按钮“{name}”",
        )
        locator.click()
        self._wait_for_page_ready(page, f"点击“{name}”")


class CTExcelBatchAutomation:
    """顺序运行多单；每单仍在微信二维码处等待人工支付。"""

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

    def stop(self) -> None:
        self.stop_event.set()
        if self.session:
            self.session.stop()

    def run(self) -> AutomationBatchResult:
        total = application_target(self.config)
        completed = min(self.completed_before, total)
        last_result: Optional[AutomationResult] = None
        if completed >= total:
            return AutomationBatchResult(
                completed_count=completed,
                total_count=total,
            )

        for ordinal in range(completed + 1, total + 1):
            if self.stop_event.is_set():
                raise AutomationError("用户已停止连续申请")
            self.item_started(ordinal, total)
            self.log(f"开始第 {ordinal} / {total} 单申请")
            self.session = self.automation_factory(
                self.config,
                log=self.log,
                stage=self.stage,
                customer_created=self.customer_created,
            )
            last_result = self.session.run()
            self.session = None
            completed = ordinal
            self.item_completed(last_result, ordinal, total)
            if ordinal >= total:
                break
            self.log(
                f"第 {ordinal} 单支付完成；"
                f"{total - ordinal} 单等待继续"
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
