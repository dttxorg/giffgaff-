from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import threading
import time
from types import SimpleNamespace

import pytest

import ctexcel_client.automation as automation_module

from ctexcel_client.automation import (
    AutomationResult,
    AutomationError,
    CTExcelBatchAutomation,
    CTExcelAutomation,
    application_target,
    address_region_token,
    append_address_suffix,
    registration_values_for_ordinal,
    assess_verification_freshness,
    cleanup_stale_browser_profiles,
    coupon_rejection_message,
    diagnostic_response_excerpt,
    normalize_money,
    parse_message_timestamp,
    is_payment_success_url,
    payment_page_has_expected_amount,
    price_is_expected,
)
from ctexcel_client.config import (
    AppConfig,
    PURCHASE_ROUTE_FREECARD,
    ProxyConfig,
    RegistrationDefaults,
)
from ctexcel_client.proxy import ProxyError


def test_money_and_discount_price_parsing():
    assert normalize_money("£ 5.95") == Decimal("5.95")
    assert normalize_money("not-a-price") is None
    assert price_is_expected("订单金额：£5.95", "5.95") is True
    assert price_is_expected("订单金额：£11.90", "5.95") is False
    assert payment_page_has_expected_amount(
        "请使用微信扫描二维码 ¥9.11(1GBP)",
        "1.00",
    )


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


def test_success_page_completes_immediately_without_reading_phone():
    class FakePage:
        url = "https://www.ctexcel.com/freecard/activityPageSuccess"

        def __getattr__(self, name):
            raise AssertionError(f"成功页不应读取页面内容：{name}")

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

    assert result.phone_number == ""
    assert result.transaction_amount == "1.00"
    assert api.saved == (
        480,
        {
            "order_number": "ORDERSUK2026073106180627794025",
            "transaction_amount": "1.00",
        },
    )
    assert any("客户端不再读取手机号" in item for item in messages)


def test_browser_profile_is_isolated_and_automation_banner_is_removed():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "automation.py"
    ).read_text(encoding="utf-8")

    assert 'tempfile.mkdtemp(' in source
    assert '"--enable-automation"' in source
    assert '"--no-sandbox"' in source
    assert "--disable-blink-features=AutomationControlled" in source
    assert "Navigator.prototype" in source
    assert "cleanup_stale_browser_profiles(profile_root)" in source
    assert "remove_browser_profile(self.profile_dir)" in source
    assert "self.context.clear_cookies()" in source
    assert "localStorage.clear()" in source
    assert "requestfinished" in source
    assert "response.text()" in source
    assert "PURCHASE_LIMIT_MARKERS" in source
    assert "error-{stamp}-network.txt" in source


def test_stale_browser_profile_cleanup_preserves_recent_runs(tmp_path: Path):
    old_profile = tmp_path / "order-old"
    recent_profile = tmp_path / "order-recent"
    unrelated = tmp_path / "manual-profile"
    old_profile.mkdir()
    recent_profile.mkdir()
    unrelated.mkdir()
    now = 1_800_000_000.0
    os.utime(old_profile, (now - 90_000, now - 90_000))
    os.utime(recent_profile, (now - 60, now - 60))
    os.utime(unrelated, (now - 90_000, now - 90_000))

    removed = cleanup_stale_browser_profiles(tmp_path, now=now)

    assert removed == 1
    assert not old_profile.exists()
    assert recent_profile.exists()
    assert unrelated.exists()


def test_diagnostic_response_excerpt_redacts_credentials():
    excerpt = diagnostic_response_excerpt(
        '{"purchase_limit":true,"AuthPwd":"secret-value",'
        '"message":"购买上限"}'
    )

    assert "purchase_limit" in excerpt
    assert "购买上限" in excerpt
    assert "secret-value" not in excerpt
    assert "<redacted>" in excerpt


def test_network_diagnostics_capture_purchase_limit_response_body():
    callbacks = {}

    class FakePage:
        def on(self, name, callback):
            callbacks[name] = callback

    class FakeResponse:
        status = 200

        def text(self):
            return (
                '{"message":"购买上限","AuthKey":"private-key"}'
            )

    class FakeRequest:
        url = "https://www.ctexcel.com/api/order/submit"
        method = "POST"
        failure = ""

        def response(self):
            return FakeResponse()

    runner = CTExcelAutomation(
        AppConfig(),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )
    runner._attach_page_diagnostics(FakePage())
    callbacks["requestfinished"](FakeRequest())

    assert len(runner.network_events) == 1
    assert "HTTP 200 POST /api/order/submit" in runner.network_events[0]
    assert "购买上限" in runner.network_events[0]
    assert "private-key" not in runner.network_events[0]
    assert "<redacted>" in runner.network_events[0]


def test_application_flow_has_no_phone_capture_or_gate():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "automation.py"
    ).read_text(encoding="utf-8")

    assert "CAPTURE_CTEXCEL_ORDER_RESPONSES_SCRIPT" not in source
    assert "XMLHttpRequest.prototype.open" not in source
    assert "window.fetch = async" not in source
    assert "CTEXCEL_ORDER_CAPTURE:" not in source
    assert "_wait_for_confirmed_phone" not in source
    assert "captured_phone_number" not in source
    assert "getStuOrderCardDetail" not in source
    assert "客户端不再读取手机号，本单立即完成" in source
    assert "订单确认接口没有返回手机号，已停止进入支付" not in source


def test_authenticated_socks_bridge_is_preflighted_before_customer_creation(
    monkeypatch,
):
    events = []

    class FakeRoute:
        proxy = {"server": "socks5://127.0.0.1:32123"}
        bridge = object()

        def close(self):
            events.append("closed")

    monkeypatch.setattr(
        automation_module,
        "prepare_proxy",
        lambda *_args, **_kwargs: SimpleNamespace(
            playwright_proxy={
                "server": "socks5://proxy.example.test:3010",
                "username": "user",
                "password": "pass",
            },
            public_ip="",
            public_ip_error="",
        ),
    )
    monkeypatch.setattr(
        automation_module,
        "browser_compatible_proxy",
        lambda _proxy: FakeRoute(),
    )

    def fail_bridge_probe(_proxy):
        events.append("bridge-probe")
        raise ProxyError("本机桥接连接失败")

    monkeypatch.setattr(
        automation_module,
        "probe_proxy_endpoint",
        fail_bridge_probe,
    )

    class UnexpectedAdminApi:
        def __init__(self, *_args, **_kwargs):
            events.append("customer-api")
            raise AssertionError("代理预检失败时不应连接客户 API")

    monkeypatch.setattr(
        automation_module,
        "AdminApi",
        UnexpectedAdminApi,
    )
    config = AppConfig(
        proxy=ProxyConfig(mode="custom"),
        registration=RegistrationDefaults(
            last_name="朱",
            first_name="先生",
            contact_phone="18170908787",
            chinese_address="测试地址",
        ),
    )
    runner = CTExcelAutomation(
        config,
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
    )

    with pytest.raises(AutomationError, match="本机桥接连接失败"):
        runner.run()

    assert events == ["bridge-probe", "closed"]


def test_browser_launch_skips_external_ip_detection():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "automation.py"
    ).read_text(encoding="utf-8")

    assert "api.ipify.org" not in source
    assert "1.1.1.1/cdn-cgi/trace" not in source
    assert "跳过第三方 IP 检测并直接进入注册" in source


def test_stopping_a_worker_releases_the_initial_browser_barrier():
    barrier = threading.Barrier(2)
    runner = CTExcelAutomation(
        AppConfig(),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
        browser_start_barrier=barrier,
    )

    runner.stop()

    assert barrier.broken is True


def test_sim_configuration_tracks_current_page_dom_and_preserves_errors():
    source = (
        Path(__file__).resolve().parents[1]
        / "ctexcel_client"
        / "automation.py"
    ).read_text(encoding="utf-8")

    assert '"实体SIM卡",\n            exact=False' in source
    assert "全部拒绝" in source
    assert ".uc-deny-button" in source
    assert "COOKIE_CONSENT_WATCHER_SCRIPT" in source
    assert "__ctexcelDismissCookieConsent" in source
    assert "已自动关闭隐私设置" in source
    assert "'全部接受'" in source
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
    assert "连续申请模式：本单已完成" in source


def test_freecard_route_is_the_new_default():
    config = AppConfig()

    assert config.purchase_route == PURCHASE_ROUTE_FREECARD
    assert config.registration.freecard_referrer == "447942946765"


def test_registration_ranges_increment_phone_and_append_address_suffix():
    defaults = RegistrationDefaults(
        contact_phone="13800000000",
        contact_phone_end="13800000999",
        chinese_address="测试省测试市测试区测试路测试驿站1111",
        address_suffix_start=1,
        address_suffix_end=1000,
    )

    assert registration_values_for_ordinal(defaults, 1) == (
        "13800000000",
        defaults.chinese_address + "1",
    )
    assert registration_values_for_ordinal(defaults, 2) == (
        "13800000001",
        defaults.chinese_address + "2",
    )
    assert registration_values_for_ordinal(defaults, 1000) == (
        "13800000999",
        defaults.chinese_address + "1000",
    )


def test_address_suffix_is_appended_after_smart_recognition():
    assert append_address_suffix("测试路88号收货点", 200) == (
        "测试路88号收货点200"
    )


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("江西省南昌市测试区测试路1号", "江西省"),
        ("北京市朝阳区测试路1号", "北京市"),
        ("内蒙古自治区呼和浩特市测试路1号", "内蒙古自治区"),
    ],
)
def test_address_region_token_tracks_configured_province(address, expected):
    assert address_region_token(address) == expected


def test_registration_ranges_fail_before_reusing_an_exhausted_value():
    defaults = RegistrationDefaults(
        contact_phone="13800000000",
        contact_phone_end="13800000001",
        chinese_address="固定地址1111",
        address_suffix_start=1,
        address_suffix_end=2,
    )

    with pytest.raises(AutomationError, match="联系电话区间不足"):
        registration_values_for_ordinal(defaults, 3)


def test_empty_phone_end_keeps_legacy_fixed_phone_but_numbers_addresses():
    defaults = RegistrationDefaults(
        contact_phone="13800000000",
        chinese_address="固定地址1111",
        address_suffix_start=1,
        address_suffix_end=1000,
    )

    assert registration_values_for_ordinal(defaults, 23) == (
        "13800000000",
        "固定地址111123",
    )


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


def test_continuous_runner_supports_ten_safe_parallel_workers():
    state_lock = threading.Lock()
    active = 0
    max_active = 0
    created = 0
    request_keys = []
    reuse_flags = []
    worker_slots = []
    browser_start_barriers = []
    completed_ordinals = []

    class FakeAutomation:
        def __init__(
            self,
            _config,
            *,
            request_key,
            reuse_pending_customer,
            worker_slot,
            browser_start_barrier,
            **_callbacks,
        ):
            nonlocal created
            with state_lock:
                created += 1
                self.customer_id = created
                request_keys.append(request_key)
                reuse_flags.append(reuse_pending_customer)
                worker_slots.append(worker_slot)
                browser_start_barriers.append(browser_start_barrier)

        def run(self):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.04)
            with state_lock:
                active -= 1
            return AutomationResult(
                customer_id=self.customer_id,
                email=f"parallel-{self.customer_id}@example.test",
            )

        def stop(self):
            pass

    runner = CTExcelBatchAutomation(
        AppConfig(
            continuous_enabled=True,
            continuous_count=12,
            continuous_workers=10,
            continuous_interval_seconds=0,
        ),
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
        item_started=lambda *_args: None,
        item_completed=lambda result, _completed, _total: (
            completed_ordinals.append(result.batch_ordinal)
        ),
        automation_factory=FakeAutomation,
    )

    result = runner.run()

    assert result.completed_count == 12
    assert max_active == 10
    assert len(set(request_keys)) == 12
    assert reuse_flags.count(True) == 1
    assert set(worker_slots) == set(range(1, 11))
    first_wave_barriers = [
        barrier for barrier in browser_start_barriers if barrier is not None
    ]
    assert len(first_wave_barriers) == 10
    assert len({id(barrier) for barrier in first_wave_barriers}) == 1
    assert browser_start_barriers.count(None) == 2
    assert sorted(completed_ordinals) == list(range(1, 13))


def test_qg_allocator_retries_duplicate_ip_and_assigns_unique_nodes(
    monkeypatch,
):
    responses = iter(
        [
            {"server": "http://198.51.100.1:10001"},
            {"server": "http://198.51.100.1:10002"},
            {"server": "http://198.51.100.2:10003"},
        ]
    )
    monkeypatch.setattr(
        "ctexcel_client.automation.resolve_proxy",
        lambda _config: next(responses),
    )
    config = AppConfig(
        proxy=ProxyConfig(
            mode="api",
            api_url="https://share.proxy.qg.net/get?num=1",
            api_key="test-key",
        )
    )
    runner = CTExcelBatchAutomation(
        config,
        log=lambda _message: None,
        stage=lambda _stage: None,
        customer_created=lambda _payload: None,
        item_started=lambda *_args: None,
        item_completed=lambda *_args: None,
    )

    first = runner._next_unique_qg_proxy()
    second = runner._next_unique_qg_proxy()

    assert first["server"] == "http://198.51.100.1:10001"
    assert second["server"] == "http://198.51.100.2:10003"
    assert runner.qg_proxy_ips == {"198.51.100.1", "198.51.100.2"}


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


def test_pending_customer_confirmation_email_prevents_reuse():
    messages = []

    class FakeApi:
        def pending_customers(self):
            return [
                {
                    "customer_id": 488,
                    "email": "confirmed@example.test",
                    "phone_number": None,
                    "order_number": None,
                    "registration_confirmed_at": None,
                }
            ]

        def sync_order_info(self, customer_id):
            assert customer_id == 488
            return {
                "found": True,
                "registration_confirmed": True,
                "registration_confirmed_at": "2026-07-31T07:05:00Z",
                "phone_number": None,
                "order_number": None,
            }

    automation = CTExcelAutomation(
        AppConfig(),
        log=messages.append,
        stage=lambda _message: None,
        customer_created=lambda _payload: None,
    )
    automation._refresh_pending_customers(FakeApi())

    assert any("标记为注册成功并跳过复用" in item for item in messages)
    assert any("后续申请将使用新客户邮箱" in item for item in messages)
