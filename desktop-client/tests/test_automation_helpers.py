from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ctexcel_client.automation import (
    AutomationResult,
    CTExcelBatchAutomation,
    CTExcelAutomation,
    application_target,
    assess_verification_freshness,
    coupon_rejection_message,
    normalize_money,
    parse_message_timestamp,
    parse_success_text,
    is_payment_success_url,
    payment_page_has_expected_amount,
    price_is_expected,
)
from ctexcel_client.config import (
    AppConfig,
    PURCHASE_ROUTE_FREECARD,
)


def test_money_and_discount_price_parsing():
    assert normalize_money("£ 5.95") == Decimal("5.95")
    assert normalize_money("not-a-price") is None
    assert price_is_expected("订单金额：£5.95", "5.95") is True
    assert price_is_expected("订单金额：£11.90", "5.95") is False
    assert payment_page_has_expected_amount(
        "请使用微信扫描二维码 ¥9.11(1GBP)",
        "1.00",
    )


def test_success_page_fields_are_read_for_operator_summary_only():
    parsed = parse_success_text(
        """
        订购成功
        订单号：ORDER2026072912345678901
        手机号码：07900000009
        交易金额：£ 5.95
        """
    )

    assert parsed == {
        "order_number": "ORDER2026072912345678901",
        "phone_number": "07900000009",
        "transaction_amount": "5.95",
    }
    freecard = parse_success_text(
        """
        支付成功
        订单号：ORDERSUK2026073104095817734376
        手机号码：07900000009
        付款金额：£ 1.00
        """
    )
    assert freecard["order_number"] == "ORDERSUK2026073104095817734376"
    assert freecard["transaction_amount"] == "1.00"


def test_both_purchase_routes_recognize_their_success_page():
    assert is_payment_success_url(
        "https://www.ctexcel.com/freecard/activityPageSuccess"
    )
    assert is_payment_success_url(
        "https://www.ctexcel.com/uk/buycard/buycardsucceed"
    )
    assert not is_payment_success_url(
        "https://www.ctexcel.com/freecard/buycardWX"
    )


def test_success_page_ignores_loading_overlay_and_saves_phone():
    class FakeBody:
        def inner_text(self, **_kwargs):
            return """
            订购成功
            订单号码：ORDERSUK2026073106180627794025
            手机号码：447434000172
            手机号码金额：£ 0.00
            """

    class FakePage:
        url = "https://www.ctexcel.com/freecard/activityPageSuccess"

        def is_closed(self):
            return False

        def wait_for_function(self, script, timeout):
            assert "phone" in script
            assert timeout == 120000

        def locator(self, selector):
            assert selector == "body"
            return FakeBody()

    class FakeApi:
        def __init__(self):
            self.saved = None

        def save_payment_checkpoint(self, customer_id, **fields):
            self.saved = (customer_id, fields)
            return {"ok": True}

    messages = []
    api = FakeApi()
    automation = CTExcelAutomation(
        AppConfig(),
        log=messages.append,
        stage=lambda _message: None,
        customer_created=lambda _payload: None,
    )

    result = automation._wait_for_payment_success(
        FakePage(),
        api=api,
        customer_id=480,
        email="customer@example.test",
        pending_order={
            "order_number": "ORDERSUK2026073106180627794025",
            "transaction_amount": "1.00",
        },
    )

    assert result.phone_number == "447434000172"
    assert result.transaction_amount == "1.00"
    assert api.saved == (
        480,
        {
            "order_number": "ORDERSUK2026073106180627794025",
            "transaction_amount": "1.00",
            "phone_number": "447434000172",
        },
    )
    assert any("忽略该页面持续显示的 Loading 遮罩" in item for item in messages)


def test_browser_profile_is_isolated_and_automation_banner_is_removed():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "automation.py"
    ).read_text(encoding="utf-8")

    assert 'tempfile.mkdtemp(' in source
    assert '"ignore_default_args": ["--enable-automation"]' in source
    assert "--disable-blink-features=AutomationControlled" in source
    assert "Navigator.prototype" in source
    assert "shutil.rmtree(self.profile_dir)" in source
    assert "requestfinished" in source
    assert "error-{stamp}-network.txt" in source


def test_success_page_phone_can_be_captured_from_finished_api_response():
    handlers = {}

    class FakePage:
        url = "https://www.ctexcel.com/freecard/activityPageSuccess"

        def on(self, name, handler):
            handlers[name] = handler

    class FakeResponse:
        status = 200
        headers = {"content-type": "application/json"}

        def text(self):
            return (
                '{"orderNo":"ORDERSUK2026073106180627794025",'
                '"phoneNumber":"447434000172"}'
            )

    class FakeRequest:
        url = "https://www.ctexcel.com/api/freecard/order/detail"
        method = "POST"
        resource_type = "xhr"

        def response(self):
            return FakeResponse()

    automation = CTExcelAutomation(
        AppConfig(),
        log=lambda _message: None,
        stage=lambda _message: None,
        customer_created=lambda _payload: None,
    )
    automation._attach_page_diagnostics(FakePage())

    handlers["requestfinished"](FakeRequest())

    assert (
        automation.captured_order_number
        == "ORDERSUK2026073106180627794025"
    )
    assert automation.captured_phone_number == "447434000172"


def test_sim_configuration_tracks_current_page_dom_and_preserves_errors():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "automation.py"
    ).read_text(encoding="utf-8")

    assert '"实体SIM卡",\n            exact=False' in source
    assert "button.uc-deny-button" in source
    assert 'page.locator(".el-switch")' in source
    assert "'.el-loading-mask'" in source
    assert "Loading 遮罩持续未消失" in source
    assert '"networkidle"' in source
    configure_start = source.index("def _configure_sim")
    switch_click = source.index("switch.click()", configure_start)
    wait_before_switch = source.index(
        'self._wait_for_page_ready(page, "自动续订开关")',
        configure_start,
    )
    assert wait_before_switch < switch_click
    assert (
        'self._wait_for_page_ready(page, "关闭自动续订")'
        in source[switch_click:]
    )
    assert "错误现场已保留" in source
    assert "error_browser_hold_seconds" in source
    assert "def _start_freecard_application" in source
    assert "FREECARD_APPLICATION_URL" in source
    assert "先预存£1领卡" in source
    assert "activityPagefillInfos" in source
    assert "activityPageconfirm" in source
    assert "freecard/buycardWX" in source
    assert "save_payment_checkpoint" in source
    assert "allow_new_after_checkpoint" in source
    assert "连续申请模式：订单邮件由服务器后台继续同步" in source


def test_freecard_route_is_the_new_default():
    config = AppConfig()

    assert config.purchase_route == PURCHASE_ROUTE_FREECARD
    assert config.registration.freecard_referrer == "447942946765"


def test_continuous_runner_waits_for_each_completed_item_before_next():
    started = []
    completed = []
    created_count = 0

    class FakeAutomation:
        def __init__(self, _config, **_callbacks):
            nonlocal created_count
            created_count += 1
            self.ordinal = created_count

        def run(self):
            return AutomationResult(
                customer_id=self.ordinal,
                email=f"customer-{self.ordinal}@example.test",
                order_number=f"ORDERSUK20260731{self.ordinal:012d}",
                transaction_amount="1.00",
            )

        def stop(self):
            pass

    config = AppConfig(
        continuous_enabled=True,
        continuous_count=3,
        continuous_interval_seconds=0,
    )
    runner = CTExcelBatchAutomation(
        config,
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
        item_started=lambda ordinal, total: started.append(
            (ordinal, total)
        ),
        item_completed=lambda result, ordinal, total: completed.append(
            (result.customer_id, ordinal, total)
        ),
        automation_factory=FakeAutomation,
    )

    result = runner.run()

    assert application_target(config) == 3
    assert started == [(1, 3), (2, 3), (3, 3)]
    assert completed == [(1, 1, 3), (2, 2, 3), (3, 3, 3)]
    assert result.completed_count == 3
    assert result.total_count == 3
    assert result.last_result.customer_id == 3


def test_continuous_runner_resumes_after_completed_items():
    started = []

    class FakeAutomation:
        def __init__(self, _config, **_callbacks):
            pass

        def run(self):
            return AutomationResult(
                customer_id=9,
                email="resume@example.test",
            )

        def stop(self):
            pass

    runner = CTExcelBatchAutomation(
        AppConfig(
            continuous_enabled=True,
            continuous_count=3,
            continuous_interval_seconds=0,
        ),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
        item_started=lambda ordinal, total: started.append(
            (ordinal, total)
        ),
        item_completed=lambda *_args: None,
        completed_before=2,
        automation_factory=FakeAutomation,
    )

    result = runner.run()

    assert started == [(3, 3)]
    assert result.completed_count == 3


def test_loading_overlay_waits_until_the_page_is_stably_ready():
    class FakePage:
        def __init__(self):
            self.values = [True, True, False, False, False, False]

        def evaluate(self, _script):
            return self.values.pop(0) if self.values else False

        def is_closed(self):
            return False

        def wait_for_load_state(self, state, timeout):
            assert state == "networkidle"
            assert timeout == 3000

    messages = []
    automation = CTExcelAutomation(
        AppConfig(page_timeout_ms=5000, step_timeout_ms=2000),
        log=messages.append,
        stage=lambda _message: None,
        customer_created=lambda _payload: None,
    )

    automation._wait_for_page_ready(
        FakePage(),
        "自动续订开关",
        stable_seconds=0.5,
    )

    assert messages == [
        "等待页面加载完成：自动续订开关",
        "页面加载完成：自动续订开关",
    ]


def test_registration_fields_target_real_inputs_instead_of_placeholder_wrappers():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "automation.py"
    ).read_text(encoding="utf-8")

    assert "page.locator(f'input[placeholder=\"{escaped}\"]')" in source
    assert 'page.get_by_placeholder("请填写姓").fill' not in source
    assert 'page.get_by_placeholder("请填写验证码").fill' not in source


def test_country_is_selected_before_registration_fields_at_human_paced_speed():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "automation.py"
    ).read_text(encoding="utf-8")

    form_start = source.index("def _fill_customer_info")
    form_end = source.index("def _poll_verification_code", form_start)
    form_source = source[form_start:form_end]
    assert form_source.index("self._select_china(page)") < form_source.index(
        '"请填写姓"'
    )
    assert '"slow_mo": max(800, int(self.config.slow_mo_ms))' in source
    assert "stable_seconds: float = 1.2" in source
    assert "' el-select '" in source


def test_verification_timestamp_parser_supports_provider_formats():
    milliseconds = parse_message_timestamp("1784918151251")
    iso_time = parse_message_timestamp("2026-07-24T18:35:51.251Z")

    assert milliseconds is not None
    assert milliseconds.timestamp() == 1784918151.251
    assert iso_time == datetime(
        2026,
        7,
        24,
        18,
        35,
        51,
        251000,
        tzinfo=timezone.utc,
    )


def test_verification_freshness_rejects_baseline_and_old_messages():
    requested_at = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

    same_message = assess_verification_freshness(
        {
            "message_id": "old-message",
            "received_at": "2026-07-29T12:00:05Z",
        },
        baseline_message_id="old-message",
        requested_at=requested_at,
    )
    old_timestamp = assess_verification_freshness(
        {
            "message_id": "different-old-message",
            "received_at": "2026-07-29T11:50:00Z",
        },
        baseline_message_id="old-message",
        requested_at=requested_at,
    )

    assert same_message[0] is False
    assert same_message[1] == "邮件 ID 与发送前相同"
    assert old_timestamp[0] is False
    assert old_timestamp[1] == "邮件收件时间早于本次请求"


def test_verification_freshness_accepts_new_message_after_request():
    requested_at = datetime.now(timezone.utc)
    fresh = assess_verification_freshness(
        {
            "message_id": "new-message",
            "received_at": (
                requested_at + timedelta(seconds=4)
            ).isoformat(),
        },
        baseline_message_id="old-message",
        requested_at=requested_at,
    )

    assert fresh[0] is True
    assert fresh[1] == "验证码邮件属于本次请求"


def test_coupon_rejection_is_reported_instead_of_looking_like_missing_input():
    assert coupon_rejection_message(
        "提示：优惠券不存在或已过期"
    ) == "优惠券不存在或已过期"
    assert coupon_rejection_message("订单金额：£5.95") == ""
