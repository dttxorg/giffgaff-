from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import contextlib
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Optional

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from .api import AdminApi, ApiError
from .config import AppConfig, app_config_dir


LogCallback = Callable[[str], None]
StageCallback = Callable[[str], None]
CustomerCallback = Callable[[dict[str, Any]], None]

ORDER_PATTERN = re.compile(r"\bORDER\d{12,}\b", re.I)
PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?44|0)7\d{9}(?!\d)")


class AutomationError(RuntimeError):
    pass


@dataclass
class AutomationResult:
    customer_id: int
    email: str
    order_number: str = ""
    phone_number: str = ""
    transaction_amount: str = ""


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


def parse_success_text(page_text: str) -> dict[str, str]:
    text = page_text or ""
    order = ORDER_PATTERN.search(text)
    phone_match = re.search(
        r"手机号码\s*[:：]\s*((?:\+?44|0)7\d{9})",
        text,
    )
    amount_match = re.search(
        r"交易金额\s*[:：]\s*£\s*([0-9]+(?:\.[0-9]{1,2})?)",
        text,
    )
    return {
        "order_number": order.group(0).upper() if order else "",
        "phone_number": phone_match.group(1) if phone_match else "",
        "transaction_amount": amount_match.group(1) if amount_match else "",
    }


class CTExcelAutomation:
    """CTExcel 购买流程。

    客户和邮箱由管理端先创建；订单号、号码等资料由管理端后台扫描订单邮件，
    客户端不直接写订单字段。
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

    def stop(self) -> None:
        self.stop_event.set()

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise AutomationError("用户已停止当前流程")

    def run(self) -> AutomationResult:
        registration = self.config.registration
        self._validate_registration_defaults()
        self.stage("连接客户管理")
        with AdminApi(
            self.config.server_url,
            self.config.app_password,
        ) as api:
            api.connect()
            self.log("客户管理连接成功")
            self.stage("准备 CTExcel 客户")
            self._refresh_pending_customers(api)
            created = api.create_ctexcel_customer()
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
            return self._run_browser(api, customer_id, email)

    def _refresh_pending_customers(self, api: AdminApi) -> None:
        """开始新流程前先扫描无手机号客户，避免重复建立空记录。"""
        pending = api.pending_customers()
        if not pending:
            self.log("没有无手机号的待完成客户，将新建客户")
            return
        self.log(f"检测到 {len(pending)} 个无手机号 CTExcel 客户，先扫描订单邮件")
        synced_count = 0
        for customer in pending[:20]:
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
        if synced_count:
            self.log(f"本轮已补全 {synced_count} 个客户的手机号")

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
        if normalize_money(defaults.expected_price_gbp) is None:
            raise AutomationError("预期优惠价格格式错误")

    def _run_browser(
        self,
        api: AdminApi,
        customer_id: int,
        email: str,
    ) -> AutomationResult:
        self.stage("启动浏览器")
        with sync_playwright() as playwright:
            launch_options: dict[str, Any] = {
                "headless": bool(self.config.headless),
                "slow_mo": max(0, int(self.config.slow_mo_ms)),
            }
            channel = (self.config.browser_channel or "").strip().lower()
            if channel and channel != "chromium":
                launch_options["channel"] = channel
            proxy = self.config.proxy.playwright_proxy()
            if proxy:
                launch_options["proxy"] = proxy
            self.context = playwright.chromium.launch_persistent_context(
                self.config.user_data_dir,
                **launch_options,
            )
            page: Optional[Page] = None
            try:
                page = self.context.pages[0] if self.context.pages else self.context.new_page()
                page.set_default_timeout(max(1000, int(self.config.step_timeout_ms)))
                page.set_default_navigation_timeout(
                    max(5000, int(self.config.page_timeout_ms))
                )
                self._select_plan(page)
                self._configure_sim(page)
                self._fill_customer_info(page, api, customer_id, email)
                self._confirm_order(page)
                pending = self._open_wechat_payment(page)
                result = self._wait_for_payment_success(
                    page,
                    customer_id=customer_id,
                    email=email,
                    pending_order=pending,
                )
                self.stage("支付成功")
                self.stage("同步号码资料")
                synced = self._poll_order_info(api, customer_id)
                if synced:
                    result.phone_number = (
                        str(synced.get("phone_number") or "").strip()
                        or result.phone_number
                    )
                    result.order_number = (
                        str(synced.get("order_number") or "").strip()
                        or result.order_number
                    )
                    result.transaction_amount = (
                        str(synced.get("transaction_amount") or "").strip()
                        or result.transaction_amount
                    )
                self.log(
                    "支付成功；客户管理后台将根据专属邮箱自动同步订单号和手机号码"
                )
                page.wait_for_timeout(5000)
                return result
            except Exception as exc:
                if page is not None:
                    self._preserve_error_page(page, exc)
                raise
            finally:
                with contextlib.suppress(Exception):
                    self.context.close()
                self.context = None

    def _preserve_error_page(self, page: Page, exc: Exception) -> None:
        """保存错误现场，并在可视模式下短暂保留浏览器供人工检查。"""
        diagnostics = Path(app_config_dir()) / "diagnostics"
        diagnostics.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        screenshot_path = diagnostics / f"error-{stamp}.png"
        html_path = diagnostics / f"error-{stamp}.html"
        with contextlib.suppress(Exception):
            page.screenshot(path=str(screenshot_path), full_page=True)
        with contextlib.suppress(Exception):
            html_path.write_text(page.content(), encoding="utf-8")

        self.stage("错误现场已保留")
        self.log(f"流程错误：{exc}")
        self.log(f"诊断文件：{diagnostics}")
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

    def _poll_order_info(
        self,
        api: AdminApi,
        customer_id: int,
    ) -> dict[str, Any]:
        """支付完成后等待订单邮件，并立即把手机号写入客户记录。"""
        timeout_seconds = max(
            15,
            int(self.config.order_sync_timeout_seconds),
        )
        deadline = time.monotonic() + timeout_seconds
        last_result: dict[str, Any] = {}
        last_error = ""
        while time.monotonic() < deadline:
            self._check_stop()
            try:
                result = api.sync_order_info(customer_id)
            except ApiError as exc:
                last_error = str(exc)
            else:
                last_result = result
                phone_number = str(result.get("phone_number") or "").strip()
                if phone_number:
                    self.log(f"手机号已从订单邮件同步：{phone_number}")
                    return result
            time.sleep(5)
        detail = str(last_result.get("detail") or last_error).strip()
        self.log(
            "订单邮件暂未出现手机号，服务器后台会继续自动扫描"
            + (f"：{detail}" if detail else "")
        )
        return last_result

    def _select_plan(self, page: Page) -> None:
        self.stage("选择 50GB 套餐")
        page.goto(
            self.config.application_url,
            wait_until="domcontentloaded",
            timeout=self.config.page_timeout_ms,
        )
        self._dismiss_cookie_consent(page)
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
        self.log("已选择 50GB、£11.9/30天套餐")

    def _configure_sim(self, page: Page) -> None:
        self.stage("配置实体卡")
        self._dismiss_cookie_consent(page)
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

        switch = self._visible_locator(
            page.locator(".el-switch"),
            "自动续订开关",
        )
        switch_class = switch.get_attribute("class") or ""
        if "is-checked" in switch_class:
            switch.click()
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
        option = self._visible_locator(
            page.get_by_text(text, exact=exact),
            label,
        )
        selected = option.evaluate(
            "element => Boolean(element.closest('.actived'))"
        )
        if not selected:
            option.click()
            page.wait_for_timeout(250)
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
        page.get_by_placeholder("请填写姓").fill(defaults.last_name.strip())
        page.get_by_placeholder("请填写名").fill(defaults.first_name.strip())
        page.get_by_placeholder("请填写邮箱").fill(email)
        page.get_by_placeholder("请填写联系电话").fill(
            defaults.contact_phone.strip()
        )
        referral = page.get_by_placeholder(
            "请填写推荐人电话/推荐号码（选填）"
        )
        referral.fill(defaults.referral_code.strip())

        self._click_visible_text(page, "获取验证码")
        self.log("验证码已请求，等待客户管理系统读取专属邮箱")
        code = self._poll_verification_code(api, customer_id)
        page.get_by_placeholder("请填写验证码").fill(code)
        self.log("验证码已自动填入")

        self._select_china(page)
        self._smart_fill_address(page, defaults.chinese_address.strip())
        self._ensure_marketing_off(page)
        self._click_button(page, "同意提交")
        page.wait_for_url(
            "**/buycard/buycardlist",
            timeout=self.config.page_timeout_ms,
        )

    def _poll_verification_code(self, api: AdminApi, customer_id: int) -> str:
        deadline = time.monotonic() + max(
            30,
            int(self.config.verification_timeout_seconds),
        )
        last_detail = ""
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
                    return code
            time.sleep(3)
        raise AutomationError(
            "等待邮箱验证码超时"
            + (f"：{last_detail}" if last_detail else "")
        )

    def _select_china(self, page: Page) -> None:
        country = page.get_by_role("combobox", name=re.compile("寄送国家"))
        if country.count() != 1:
            raise AutomationError("寄送国家下拉框数量异常")
        country.click()
        option = page.get_by_role("option", name="中国", exact=True)
        option.wait_for(state="visible")
        option.click()
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

    def _confirm_order(self, page: Page) -> None:
        self.stage("应用半价优惠")
        defaults = self.config.registration
        coupon = page.get_by_role("textbox", name="请输入", exact=True)
        if coupon.count() != 1:
            raise AutomationError("优惠码输入框数量异常")
        coupon.fill(defaults.coupon_code.strip())
        self._click_button(page, "使用优惠码")
        expected = defaults.expected_price_gbp.strip()
        try:
            page.wait_for_function(
                """expected => {
                  const text = (document.body?.innerText || '').replace(/\\s+/g, '');
                  return text.includes(`订单金额：£${expected}`);
                }""",
                arg=expected,
                timeout=self.config.step_timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            raise AutomationError("优惠码应用后没有出现预期半价") from exc
        body_text = page.locator("body").inner_text()
        if not price_is_expected(body_text, expected):
            raise AutomationError(
                f"订单价格校验失败，预期 £{expected}"
            )
        self.log(f"优惠码已生效，最终价格 £{expected}")

        other_payment = page.get_by_role(
            "radio",
            name="其他支付方式订购",
            exact=True,
        )
        if not other_payment.is_checked():
            other_payment.check()
        wechat = self._visible_locator(
            page.get_by_text("微信", exact=True),
            "微信支付方式",
        )
        wechat.click()
        selected = wechat.evaluate(
            """el => Boolean(
              el.closest('.actived')
              || el.parentElement?.classList.contains('actived')
            )"""
        )
        if not selected:
            raise AutomationError("微信支付方式没有进入选中状态")
        self.log("支付方式已切换为微信")

    def _open_wechat_payment(self, page: Page) -> dict[str, str]:
        self.stage("确认支付条款")
        self._click_button(page, "确认支付")
        dialogs = page.get_by_role("dialog")
        dialogs.first.wait_for(
            state="visible",
            timeout=self.config.step_timeout_ms,
        )
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
        page.wait_for_url(
            "**/buycard/buycardWX",
            timeout=self.config.page_timeout_ms,
        )
        page.wait_for_function(
            "() => document.body && document.body.innerText.includes('订单号码')",
            timeout=self.config.page_timeout_ms,
        )
        page_text = page.locator("body").inner_text()
        order = ORDER_PATTERN.search(page_text)
        expected = self.config.registration.expected_price_gbp.strip()
        if not price_is_expected(page_text, expected):
            raise AutomationError("微信支付页的英镑金额与优惠后价格不一致")
        order_number = order.group(0).upper() if order else ""
        self.stage("等待人工微信支付")
        self.log(
            f"微信二维码已显示，金额 £{expected}"
            + (f"，订单号 {order_number}" if order_number else "")
        )
        return {"order_number": order_number}

    def _wait_for_payment_success(
        self,
        page: Page,
        *,
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
            if "/buycard/buycardsucceed" in page.url:
                page.wait_for_function(
                    "() => document.body && document.body.innerText.includes('订购成功')",
                    timeout=self.config.page_timeout_ms,
                )
                parsed = parse_success_text(page.locator("body").inner_text())
                order_number = (
                    parsed["order_number"]
                    or pending_order.get("order_number", "")
                )
                return AutomationResult(
                    customer_id=customer_id,
                    email=email,
                    order_number=order_number,
                    phone_number=parsed["phone_number"],
                    transaction_amount=parsed["transaction_amount"],
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
        locator = self._visible_locator(
            page.get_by_text(text, exact=True),
            text,
        )
        locator.click()

    def _click_button(self, page: Page, name: str) -> None:
        locator = self._visible_locator(
            page.get_by_role("button", name=name, exact=True),
            f"按钮“{name}”",
        )
        locator.click()
