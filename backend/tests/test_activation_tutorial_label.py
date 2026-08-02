"""未激活卡教程二维码与独立打印模板回归测试。"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

import crud
import database
import main


@pytest.fixture
def client():
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "test.db")
        original = (
            database.DATABASE_PATH,
            crud.DATABASE_PATH,
            main.DATABASE_PATH,
            main.APP_PASSWORD,
            main.ADMIN_ENTRY_PATH,
        )
        database.DATABASE_PATH = db_path
        crud.DATABASE_PATH = db_path
        main.DATABASE_PATH = db_path
        main.APP_PASSWORD = ""
        main.ADMIN_ENTRY_PATH = ""
        asyncio.run(database.init_db())
        test_client = TestClient(main.app)
        try:
            yield test_client
        finally:
            test_client.close()
            (
                database.DATABASE_PATH,
                crud.DATABASE_PATH,
                main.DATABASE_PATH,
                main.APP_PASSWORD,
                main.ADMIN_ENTRY_PATH,
            ) = original


def _tutorial_template(config: dict) -> dict:
    return next(t for t in config["templates"] if t["id"] == "activation-guide-50x40")


def test_default_label_config_includes_activation_tutorial_template(client):
    response = client.get("/api/label-config")

    assert response.status_code == 200
    config = response.json()
    assert config["default_template_id"] == "basic-50x30"
    assert config["activation_tutorial_url"] == "https://gg.681218.xyz/activation.html"
    template = _tutorial_template(config)
    assert template["name"] == "未激活卡教程 50x40"
    sources = [element["source"] for element in template["elements"]]
    assert "激活教程地址" not in sources
    assert "激活教程二维码" in sources
    title = next(element for element in template["elements"] if element["id"] == "activation-url")
    assert title["source"] == "固定文字"
    assert title["text"] == "giffgaff 12 步激活教程"
    assert "手机号" not in sources
    assert "邮箱" not in sources


def test_existing_saved_templates_are_merged_with_tutorial_template(client):
    asyncio.run(crud.set_setting("label_templates", json.dumps([
        {
            "id": "existing-template",
            "name": "原模板",
            "width_mm": 50,
            "height_mm": 40,
            "elements": [],
        }
    ], ensure_ascii=False)))

    config = client.get("/api/label-config").json()

    ids = {template["id"] for template in config["templates"]}
    assert "existing-template" in ids
    assert "activation-guide-50x40" in ids


def test_tutorial_url_can_be_saved_independently(client):
    config = client.get("/api/label-config").json()
    config["activation_tutorial_url"] = "https://example.com/new-activation-guide"

    saved = client.put("/api/label-config", json=config)

    assert saved.status_code == 200
    reloaded = client.get("/api/label-config").json()
    assert reloaded["activation_tutorial_url"] == "https://example.com/new-activation-guide"
    assert reloaded["giffgaff_download_url"] == config["giffgaff_download_url"]


def test_default_label_template_can_be_saved_but_courier_cannot_be_default(client):
    config = client.get("/api/label-config").json()
    config["default_template_id"] = "full-50x40"

    saved = client.put("/api/label-config", json=config)

    assert saved.status_code == 200
    assert client.get("/api/label-config").json()["default_template_id"] == "full-50x40"

    config["default_template_id"] = "courier-50x40"
    rejected = client.put("/api/label-config", json=config)
    assert rejected.status_code == 400


def test_default_label_template_is_included_in_backup_and_restore(client):
    config = client.get("/api/label-config").json()
    config["default_template_id"] = "full-50x40"
    assert client.put("/api/label-config", json=config).status_code == 200

    backup = client.get("/api/export").json()
    assert backup["settings"]["default_label_template_id"] == "full-50x40"

    config["default_template_id"] = "basic-50x30"
    assert client.put("/api/label-config", json=config).status_code == 200
    restored = client.post(
        "/api/import",
        files={"file": ("backup.json", json.dumps(backup), "application/json")},
    )
    assert restored.status_code == 200
    assert client.get("/api/label-config").json()["default_template_id"] == "full-50x40"


def test_system_settings_exposes_tutorial_url(client):
    response = client.patch("/api/settings", json={
        "activation_tutorial_url": "https://example.com/tutorial.html",
    })

    assert response.status_code == 200
    settings = client.get("/api/settings").json()
    assert settings["activation_tutorial_url"] == "https://example.com/tutorial.html"


def test_shared_tutorial_qr_page_embeds_complete_guide_without_second_jump(client):
    client.patch("/api/settings", json={
        "activation_tutorial_url": "https://gg.681218.xyz/activation.html",
        "activation_page_markdown": "LEGACY_ACTIVATION_CONTENT",
        "public_page_markdown": "LEGACY_ACTIVATED_CONTENT",
    })

    response = client.get("/p/activation-guide-public-page")

    assert response.status_code == 200
    body = response.text
    assert 'data-url=' not in body
    assert "copyTutorialUrl" not in body
    assert "tutorial-copy-btn" not in body
    assert "https://gg.681218.xyz/activation.html" not in body
    assert 'href="https://www.giffgaff.com/activate"' in body
    assert "12 步完成激活" in body
    assert body.count('class="tutorial-step"') == 12
    assert body.count('class="step-shot"') == 10
    assert body.count("data:image/") == 11  # 10 张教程截图 + 1 张微信二维码
    assert "打开官方激活入口" in body
    assert "填写初始邮箱" in body
    assert "选择 Pay as you go" in body
    assert "不要重复点击 Place order" in body
    assert "登录官网保存号码" in body
    assert "在注册邮箱中查找 giffgaff 的激活完成或欢迎邮件" in body
    assert "不建议通过发送短信查询号码" in body
    assert "43430" not in body
    assert "class=\"step-promo\"" in body
    assert "没有可用的海外支付银行卡" in body
    assert "可联系客服办理 giffgaff 代充值" in body
    assert "LEGACY_ACTIVATION_CONTENT" not in body
    assert "LEGACY_ACTIVATED_CONTENT" not in body
    assert "giffgaff 套餐充值服务" in body
    assert "ChatGPT Plus" in body
    assert "ChatGPT 5x Pro" in body
    assert "ChatGPT 20x Pro" in body
    assert "iMessage" in body
    assert "RCS" in body
    assert "请勿在京东咨询" in body
    assert "号码保号提醒服务" in body
    assert "<!--email_off-->" in body
    assert 'class="route-strip"' in body
    assert "扫码一次" in body
    assert "激活到底" in body


def test_both_public_pages_ignore_legacy_markdown_settings(client):
    asyncio.run(crud.set_setting("activation_page_markdown", "LEGACY_TUTORIAL_TEXT"))
    asyncio.run(crud.set_setting("public_page_markdown", "LEGACY_ACTIVATED_TEXT"))
    customer_id = asyncio.run(crud.create_customer(main.CustomerCreate(
        email="contact@example.com",
        activation_date="2026-07-16",
    )))
    asyncio.run(crud.update_customer(
        customer_id,
        main.CustomerUpdate(phone_number="447400123456"),
    ))
    customer_token = client.get(f"/api/customers/{customer_id}").json()["public_token"]

    tutorial_page = client.get("/p/activation-guide-public-page").text
    activated_page = client.get(f"/p/{customer_token}").text

    assert "LEGACY_TUTORIAL_TEXT" not in tutorial_page
    assert "LEGACY_ACTIVATED_TEXT" not in tutorial_page
    assert "LEGACY_TUTORIAL_TEXT" not in activated_page
    assert "LEGACY_ACTIVATED_TEXT" not in activated_page
    assert "插卡前重要提醒" in tutorial_page
    assert "插卡前重要提醒" in activated_page
    assert "账号登录信息" not in activated_page
    assert "giffgaff 手机号码（官网账号）" in activated_page
    assert "初始注册邮箱（官网登录密码）" in activated_page


def test_both_public_pages_embed_prominent_voicemail_shutdown_guide(client):
    customer_id = asyncio.run(crud.create_customer(main.CustomerCreate(
        email="voicemail@example.com",
        activation_date="2026-07-16",
    )))
    customer_token = client.get(f"/api/customers/{customer_id}").json()["public_token"]

    pages = [
        client.get("/p/activation-guide-public-page").text,
        client.get(f"/p/{customer_token}").text,
    ]
    for body in pages:
        assert "激活后尽快关闭语音信箱" in body
        assert "可能只能等待系统自动结束" in body
        assert "国际漫游扣费风险" in body
        assert "打开 giffgaff 客服表单" in body
        assert "等待邮件确认" in body
        assert "Please fully disable voicemail" in body
        assert "copyVoicemailRequest(this)" in body
        assert "https://gg.681218.xyz/voicemail.html" not in body


def test_both_public_pages_show_cropped_wechat_support_qr(client):
    customer_id = asyncio.run(crud.create_customer(main.CustomerCreate(
        email="wechat@example.com",
        activation_date="2026-07-16",
    )))
    customer_token = client.get(f"/api/customers/{customer_id}").json()["public_token"]

    pages = [
        client.get("/p/activation-guide-public-page").text,
        client.get(f"/p/{customer_token}").text,
    ]
    for body in pages:
        assert body.count('class="wechat-card"') == 1
        assert 'alt="微信客服二维码"' in body
        assert 'class="wechat-qr-crop"' in body
        assert "width: 130.588%" in body
        assert "translate(-11.712%, -24.138%)" in body
        assert "长按识别" in body
        assert "微信扫一扫" in body
        assert "按住二维码约 2 秒" in body
        assert "猫不肥" not in body
        assert "阿富汗" not in body


def test_activation_page_version_increments_to_invalidate_worker_cache(client):
    version_url = "/api/public/activation-guide-public-page/version"
    assert client.get(version_url).json() == {"public_version": 5_000_001}

    client.patch("/api/settings", json={
        "activation_page_markdown": "第一次修改",
    })
    assert client.get(version_url).json() == {"public_version": 5_000_002}

    # 保存相同内容不应制造额外缓存版本。
    client.patch("/api/settings", json={
        "activation_page_markdown": "第一次修改",
    })
    assert client.get(version_url).json() == {"public_version": 5_000_002}

    client.patch("/api/settings", json={
        "activation_tutorial_url": "https://example.com/new-guide",
    })
    assert client.get(version_url).json() == {"public_version": 5_000_003}


@pytest.mark.parametrize("endpoint,payload", [
    ("/api/settings", {"activation_tutorial_url": "javascript:alert(1)"}),
    ("/api/label-config", {
        "giffgaff_download_url": "https://www.giffgaff.com/mobile-app",
        "activation_tutorial_url": "not-a-url",
        "templates": [],
    }),
])
def test_tutorial_url_rejects_non_http_schemes(client, endpoint, payload):
    method = client.patch if endpoint == "/api/settings" else client.put
    response = method(endpoint, json=payload)
    assert response.status_code == 400


def test_frontend_has_selectable_tutorial_sources_and_template():
    html = (ROOT_DIR / "frontend" / "index.html").read_text(encoding="utf-8")

    assert "onclick=\"addLabelElement('qr', '激活教程二维码')\"" in html
    assert "onclick=\"addLabelElement('text', '激活教程地址')\"" not in html
    assert 'id="s-activation-tutorial-url"' not in html
    assert "case '激活教程地址':" in html
    assert "source === '激活教程二维码'" in html
    assert "ACTIVATION_GUIDE_PUBLIC_TOKEN = 'activation-guide-public-page'" in html
    assert "const PUBLIC_PAGE_VIEW_VERSION = '7';" in html
    assert "return getPublicPageUrl(ACTIVATION_GUIDE_PUBLIC_TOKEN);" in html
    assert "?view=${PUBLIC_PAGE_VIEW_VERSION}" in html
    assert "onclick=\"addLabelElement('qr', '号码资料二维码')\"" in html
    assert "const PUBLIC_ACCOUNT_QR_SOURCE = '号码资料二维码';" in html
    assert 'id="s-activation-page-markdown"' not in html
    assert 'id="s-public-page-markdown"' not in html


def test_frontend_uses_customer_inbox_workspace_and_independent_print_flows():
    html = (ROOT_DIR / "frontend" / "index.html").read_text(encoding="utf-8")

    customer_start = html.index('id="tab-customers"')
    sim_start = html.index('id="tab-sim-codes"')
    detail_start = html.index('id="detail-panel"')
    assert customer_start < detail_start < sim_start
    assert "Customer Inbox" in html
    assert 'class="customer-list-pane"' in html
    assert 'class="customer-ledger-table"' in html
    assert 'id="customer-phone-filter"' in html
    assert 'id="customer-activation-filter"' in html
    assert 'id="customer-payment-filter"' in html
    assert 'id="customer-shipping-filter"' in html
    assert 'id="customer-pagination"' in html
    assert 'id="customer-page-prev"' in html
    assert 'id="customer-page-buttons"' in html
    assert 'id="customer-page-next"' in html
    assert 'id="customer-page-size"' in html
    assert "function customerMatchesFilters" in html
    assert "function bindCustomerFilters" in html
    assert "let customerPage = 1;" in html
    assert "let customerPageSize = 20;" in html
    assert "function renderCustomerPagination" in html
    assert "function bindCustomerPagination" in html
    assert "visibleCustomers.slice(pageStart, pageStart + customerPageSize)" in html
    assert "grid-template-columns: var(--workspace-list)" not in html
    assert "#detail-panel.open { display: flex; }" in html
    assert "#print-panel { z-index: 260; }" in html
    assert 'class="detail-section detail-tool-section"' in html
    assert "function toggleAddCustomer" in html
    assert "function deleteActiveCustomer" in html
    assert "function setDefaultTemplate" in html
    assert "labelConfig.default_template_id" in html
    assert "打印标签" in html
    assert "打印快递单" in html
    assert "templateId === COURIER_TEMPLATE_ID ? 'courier' : 'label'" in html
    assert "body.detail-mobile-open" in html
    assert "detailScroller.scrollTop = 0" in html
    assert "viewButton.addEventListener('click', event => event.stopPropagation(), { capture: true })" not in html
    assert 'id="d-registration-email"' in html
    assert 'id="d-registration-sim-code"' in html
    assert 'id="d-registration-password"' not in html
    assert 'id="d-code-box"' in html
    assert "function renderRegistrationQuick" in html
    assert "function toggleRegistrationPassword" not in html
    assert "function copyRegistrationPassword" not in html
    assert 'data-tab="inbox" type="button">完整邮箱</button>' in html
    assert 'id="d-inbox-refresh"' in html
    assert 'id="d-inbox-list"' in html
    assert 'id="d-inbox-message-body"' in html
    assert 'id="d-inbox-html-frame"' in html
    assert 'id="d-inbox-load-images"' in html
    assert 'sandbox="allow-popups allow-popups-to-escape-sandbox"' in html
    assert 'referrerpolicy="no-referrer"' in html
    assert "function sanitizeInboxHtml" in html
    assert "function safeInboxUrl" in html
    assert "function renderInboxTextBody" in html
    assert "function loadInboxRemoteImages" in html
    assert "noopener noreferrer nofollow" in html
    assert "['https:', 'http:', 'mailto:', 'tel:']" in html
    assert "img-src ${imageSources}" in html
    assert "connect-src 'none'" in html
    assert "function setInboxBodyMode" in html
    assert "function formatInboxDualTime" in html
    assert "'Asia/Shanghai'" in html
    assert "'Europe/London'" in html
    assert "function loadInboxForActive" in html
    assert "function loadInboxMessage" in html
    assert "/inbox-message?${query}" in html
    assert "不经过 Worker 公开页面" in html
    payment_refresh = html[html.index("async function refreshPaymentInfoForActive"):html.index("async function refreshVerificationCodeForActive")]
    assert "await loadCustomers();" in payment_refresh
    assert "activeCustomer = await getCustomer(customerId);" in payment_refresh
    assert "const TEXT_SOURCES = ['固定文字', '手机号', '邮箱', '开通日期', '收货地址', '快递公司', '快递单号', 'SIM激活码', '激活状态', 'CTExcel订单号', 'CTExcel推荐码', 'Giffgaff下载链接'];" in html
    assert "element.type === 'text' && element.source === '初始密码'" in html
    assert '<th class="th-address">收货地址</th>' in html
    assert "addCell('收货地址', 'ledger-address ledger-ellipsis')" in html
    assert '<th class="th-amount" data-mode-only="ctexcel">付款金额</th>' in html
    assert "addCell('付款金额', 'mono ledger-amount')" in html
    assert "cell.colSpan = 11;" in html
    assert "$('d-identity-disclosure').open = false;" in html
    assert '<details class="identity-disclosure" id="d-identity-disclosure" data-mode-only="giffgaff">' in html
    info_section = html.index('<section class="tab-content active detail-section" data-tab="info">')
    activation_section = html.index('<section class="tab-content detail-section" data-tab="activation" data-mode-only="giffgaff">')
    verification_box = html.index('id="d-code-box"')
    identity_disclosure = html.index('id="d-identity-disclosure"')
    assert info_section < verification_box < identity_disclosure < activation_section
    assert "发件地址" not in html
    assert 'id="s-custom-public-vars"' not in html
    assert "bindQuickInsertButtons" not in html
    assert "bindCustomVarAdd" not in html
    assert "id: 'activation-guide-50x40'" in html
