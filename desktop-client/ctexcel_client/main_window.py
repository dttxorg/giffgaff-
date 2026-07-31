from __future__ import annotations

from dataclasses import replace
import re
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QCloseEvent, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .api import AdminApi
from .automation import (
    AutomationBatchResult,
    AutomationResult,
    CTExcelBatchAutomation,
    application_target,
)
from .config import (
    AppConfig,
    ProxyConfig,
    PURCHASE_ROUTE_50GB,
    PURCHASE_ROUTE_FREECARD,
    RegistrationDefaults,
    TelegramConfig,
    is_cliproxy_whitelist_url,
    is_qg_proxy_api_url,
    load_config,
    save_config,
)
from .telegram import TelegramNotifier
from .proxy import (
    ProxyError,
    masked_proxy_label,
    parse_proxy_payload,
    prepare_proxy,
)


class ApiWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.fn = fn

    def run(self) -> None:
        try:
            self.succeeded.emit(self.fn())
        except Exception as exc:  # noqa: BLE001 - UI boundary
            self.failed.emit(str(exc))


class AutomationWorker(QThread):
    log_message = Signal(str)
    stage_changed = Signal(str)
    customer_created = Signal(object)
    item_started = Signal(int, int)
    item_completed = Signal(object, int, int)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        config: AppConfig,
        *,
        completed_before: int = 0,
    ):
        super().__init__()
        self.config = config
        self.completed_before = completed_before
        self.session: Optional[CTExcelBatchAutomation] = None

    def stop(self) -> None:
        if self.session:
            self.session.stop()

    def run(self) -> None:
        try:
            self.session = CTExcelBatchAutomation(
                self.config,
                log=self.log_message.emit,
                stage=self.stage_changed.emit,
                customer_created=self.customer_created.emit,
                item_started=self.item_started.emit,
                item_completed=self.item_completed.emit,
                completed_before=self.completed_before,
            )
            self.succeeded.emit(self.session.run())
        except Exception as exc:  # noqa: BLE001 - thread boundary
            self.failed.emit(str(exc))
        finally:
            self.session = None


class MainWindow(QMainWindow):
    STAGES = [
        "准备浏览器代理",
        "连接客户管理",
        "准备 CTExcel 客户",
        "启动浏览器",
        "等待并发窗口就绪",
        "选择申请路线",
        "配置 SIM / 套餐",
        "填写客户资料",
        "确认订单",
        "确认支付条款",
        "等待人工微信支付",
        "支付成功",
    ]

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.api_worker: Optional[ApiWorker] = None
        self.proxy_worker: Optional[ApiWorker] = None
        self.telegram_worker: Optional[ApiWorker] = None
        self.automation_worker: Optional[AutomationWorker] = None
        self.current_customer: Optional[dict[str, Any]] = None
        self.current_public_ip = ""
        self.batch_completed_count = 0
        self.batch_target_count = 1
        self.batch_resume_pending = False
        self.batch_signature: Optional[tuple[object, ...]] = None
        self.setWindowTitle(f"CTExcel 申请工作台 v{__version__}")
        self.resize(1120, 760)
        self.setMinimumSize(900, 650)
        self._build_ui()
        self._load_config()
        self._apply_style()

    @staticmethod
    def _card(title: str, subtitle: str = "") -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(14)

        heading = QLabel(title)
        heading.setObjectName("cardTitle")
        layout.addWidget(heading)
        if subtitle:
            description = QLabel(subtitle)
            description.setObjectName("cardSubtitle")
            description.setWordWrap(True)
            layout.addWidget(description)
        return card, layout

    @staticmethod
    def _field_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("fieldLabel")
        return label

    @staticmethod
    def _step(number: str, title: str, detail: str) -> QFrame:
        step = QFrame()
        step.setObjectName("stepCard")
        layout = QVBoxLayout(step)
        layout.setContentsMargins(12, 11, 12, 11)
        layout.setSpacing(4)
        number_label = QLabel(number)
        number_label.setObjectName("stepNumber")
        title_label = QLabel(title)
        title_label.setObjectName("stepTitle")
        detail_label = QLabel(detail)
        detail_label.setObjectName("stepDetail")
        detail_label.setWordWrap(True)
        layout.addWidget(number_label)
        layout.addWidget(title_label)
        layout.addWidget(detail_label)
        return step

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(28, 22, 28, 24)
        root_layout.setSpacing(18)

        header = QHBoxLayout()
        header.setSpacing(14)
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        eyebrow = QLabel("CTEXCEL · OPERATIONS")
        eyebrow.setObjectName("eyebrow")
        title = QLabel("自动申请工作台")
        title.setObjectName("title")
        subtitle = QLabel(
            "先建档并领取专属邮箱，再完成套餐申请；支付后由服务器自动同步订单资料。"
        )
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)
        title_box.addWidget(eyebrow)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)

        self.connection_pill = QLabel("●  等待连接")
        self.connection_pill.setObjectName("connectionPill")
        self.connection_pill.setProperty("state", "pending")
        self.connection_pill.setAlignment(Qt.AlignCenter)
        header.addWidget(self.connection_pill, 0, Qt.AlignTop)
        root_layout.addLayout(header)

        scroll = QScrollArea()
        scroll.setObjectName("mainScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        content = QWidget()
        content.setObjectName("scrollContent")
        body = QGridLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setHorizontalSpacing(16)
        body.setVerticalSpacing(16)
        body.setColumnStretch(0, 5)
        body.setColumnStretch(1, 6)

        left = QVBoxLayout()
        left.setSpacing(16)
        left.addWidget(self._connection_card())
        left.addWidget(self._proxy_card())
        left.addWidget(self._telegram_card())
        left.addWidget(self._registration_card())
        left.addStretch(1)

        right = QVBoxLayout()
        right.setSpacing(16)
        right.addWidget(self._workflow_card())
        right.addWidget(self._log_card(), 1)

        body.addLayout(left, 0, 0)
        body.addLayout(right, 0, 1)
        scroll.setWidget(content)
        root_layout.addWidget(scroll, 1)
        self.setCentralWidget(root)

    def _connection_card(self) -> QFrame:
        card, layout = self._card(
            "服务器连接",
            "使用独立的 CTExcel 限权 API，不再填写或访问隐藏管理入口。",
        )
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)

        self.server_url = QLineEdit()
        self.server_url.setPlaceholderText("https://你的服务器域名")
        self.server_url.setClearButtonEnabled(True)
        self.server_url.textChanged.connect(self._connection_fields_changed)

        self.app_password = QLineEdit()
        self.app_password.setEchoMode(QLineEdit.Password)
        self.app_password.setPlaceholderText("填写后台 APP_PASSWORD")
        self.app_password.textChanged.connect(self._connection_fields_changed)

        grid.addWidget(self._field_label("服务器地址"), 0, 0)
        grid.addWidget(self.server_url, 1, 0, 1, 2)
        grid.addWidget(self._field_label("客户端连接口令"), 2, 0)
        grid.addWidget(self.app_password, 3, 0, 1, 2)

        self.remember_credentials = QCheckBox(
            "使用 Windows 加密保存连接口令和 Bot Token"
        )
        self.test_connection_btn = QPushButton("测试连接")
        self.test_connection_btn.setObjectName("secondaryButton")
        self.test_connection_btn.clicked.connect(self.test_connection)
        grid.addWidget(self.remember_credentials, 4, 0)
        grid.addWidget(self.test_connection_btn, 4, 1, Qt.AlignRight)
        grid.setColumnStretch(0, 1)
        layout.addLayout(grid)

        self.connection_detail = QLabel(
            "连接后会显示服务器内现有的 CTExcel 客户数量。"
        )
        self.connection_detail.setObjectName("inlineNote")
        self.connection_detail.setWordWrap(True)
        layout.addWidget(self.connection_detail)
        return card

    def _telegram_card(self) -> QFrame:
        card, layout = self._card(
            "Telegram 付款提醒",
            "微信付款页生成后自动截取二维码，并发送到指定 Bot 会话。",
        )
        self.telegram_enabled = QCheckBox("启用付款二维码推送")
        self.telegram_enabled.toggled.connect(
            self._update_telegram_fields
        )
        layout.addWidget(self.telegram_enabled)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        self.telegram_bot_token = QLineEdit()
        self.telegram_bot_token.setEchoMode(QLineEdit.Password)
        self.telegram_bot_token.setPlaceholderText(
            "123456789:AA..."
        )
        self.telegram_chat_id = QLineEdit()
        self.telegram_chat_id.setPlaceholderText(
            "个人、群组 Chat ID 或 @channel"
        )
        self.telegram_bot_token_label = self._field_label("Bot Token")
        self.telegram_chat_id_label = self._field_label("Chat ID")
        grid.addWidget(self.telegram_bot_token_label, 0, 0)
        grid.addWidget(self.telegram_chat_id_label, 0, 1)
        grid.addWidget(self.telegram_bot_token, 1, 0)
        grid.addWidget(self.telegram_chat_id, 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        row = QHBoxLayout()
        self.telegram_status = QLabel(
            "启用后，每个线程会分别发送对应订单的二维码。"
        )
        self.telegram_status.setObjectName("inlineNote")
        self.telegram_status.setWordWrap(True)
        self.telegram_test_btn = QPushButton("测试推送")
        self.telegram_test_btn.setObjectName("secondaryButton")
        self.telegram_test_btn.clicked.connect(self.test_telegram)
        row.addWidget(self.telegram_status, 1)
        row.addWidget(self.telegram_test_btn)
        layout.addLayout(row)
        return card

    def _proxy_card(self) -> QFrame:
        card, layout = self._card(
            "浏览器代理",
            "支持 HTTP / SOCKS5 单条、批量代理池和 API 动态提取。",
        )
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)

        self.proxy_mode = QComboBox()
        self.proxy_mode.addItem("直连", "none")
        self.proxy_mode.addItem("粘贴单条代理", "custom")
        self.proxy_mode.addItem("批量代理池", "pool")
        self.proxy_mode.addItem("API 动态提取", "api")
        self.proxy_mode.currentIndexChanged.connect(self._update_proxy_fields)

        self.proxy_type = QComboBox()
        self.proxy_type.addItem("SOCKS5", "socks5")
        self.proxy_type.addItem("HTTP", "http")
        self.proxy_type.addItem("HTTPS", "https")

        self.proxy_import = QLineEdit()
        self.proxy_import.setPlaceholderText(
            "直接粘贴 hostname:port:username:password"
        )
        self.proxy_import.setClearButtonEnabled(True)
        self.proxy_import.returnPressed.connect(
            lambda: self._apply_proxy_line(show_error=True)
        )
        self.proxy_import_btn = QPushButton("从剪贴板导入")
        self.proxy_import_btn.setObjectName("miniButton")
        self.proxy_import_btn.clicked.connect(self.import_proxy_from_clipboard)

        self.proxy_pool = QPlainTextEdit()
        self.proxy_pool.setPlaceholderText(
            "每行一个代理，例如：\n"
            "hostname:port:username:password\n"
            "hostname2:port:username:password"
        )
        self.proxy_pool.setMinimumHeight(112)
        self.proxy_pool_import_btn = QPushButton("粘贴代理池")
        self.proxy_pool_import_btn.setObjectName("miniButton")
        self.proxy_pool_import_btn.clicked.connect(
            self.import_proxy_pool_from_clipboard
        )
        self.proxy_pool_uses_min = QSpinBox()
        self.proxy_pool_uses_min.setRange(1, 100)
        self.proxy_pool_uses_min.setValue(5)
        self.proxy_pool_uses_max = QSpinBox()
        self.proxy_pool_uses_max.setRange(1, 100)
        self.proxy_pool_uses_max.setValue(8)

        self.proxy_host = QLineEdit()
        self.proxy_host.setPlaceholderText("代理主机或 IP")
        self.proxy_port = QLineEdit()
        self.proxy_port.setPlaceholderText("端口")
        self.proxy_username = QLineEdit()
        self.proxy_username.setPlaceholderText("可选")
        self.proxy_password = QLineEdit()
        self.proxy_password.setPlaceholderText("可选")
        self.proxy_password.setEchoMode(QLineEdit.Password)
        self.proxy_api_url = QLineEdit()
        self.proxy_api_url.setPlaceholderText(
            "https://share.proxy.qg.net/get?num=1&distinct=true"
        )
        self.proxy_api_url.setClearButtonEnabled(True)
        self.proxy_api_key = QLineEdit()
        self.proxy_api_key.setPlaceholderText("产品唯一标识 key")
        self.proxy_api_key.setEchoMode(QLineEdit.Password)
        self.proxy_api_key.setClearButtonEnabled(True)

        self.proxy_mode_label = self._field_label("代理模式")
        self.proxy_type_label = self._field_label("代理协议")
        self.proxy_import_label = self._field_label(
            "整行代理（推荐，无需逐项填写）"
        )
        self.proxy_pool_label = self._field_label(
            "代理池（每行一条，自动去重）"
        )
        self.proxy_pool_uses_min_label = self._field_label("每个代理最少使用")
        self.proxy_pool_uses_max_label = self._field_label("每个代理最多使用")
        self.proxy_host_label = self._field_label("已解析地址")
        self.proxy_port_label = self._field_label("已解析端口")
        self.proxy_username_label = self._field_label("已解析账号")
        self.proxy_password_label = self._field_label("已解析密码")
        self.proxy_api_url_label = self._field_label("提取接口")
        self.proxy_api_key_label = self._field_label("青果代理 API Key")

        grid.addWidget(self.proxy_mode_label, 0, 0)
        grid.addWidget(self.proxy_type_label, 0, 1)
        grid.addWidget(self.proxy_mode, 1, 0)
        grid.addWidget(self.proxy_type, 1, 1)
        grid.addWidget(self.proxy_import_label, 2, 0, 1, 2)
        import_row = QHBoxLayout()
        import_row.setSpacing(8)
        import_row.addWidget(self.proxy_import, 1)
        import_row.addWidget(self.proxy_import_btn)
        grid.addLayout(import_row, 3, 0, 1, 2)
        grid.addWidget(self.proxy_pool_label, 4, 0, 1, 2)
        grid.addWidget(self.proxy_pool, 5, 0, 1, 2)
        grid.addWidget(self.proxy_pool_import_btn, 6, 0, 1, 2)
        grid.addWidget(self.proxy_pool_uses_min_label, 7, 0)
        grid.addWidget(self.proxy_pool_uses_max_label, 7, 1)
        grid.addWidget(self.proxy_pool_uses_min, 8, 0)
        grid.addWidget(self.proxy_pool_uses_max, 8, 1)
        grid.addWidget(self.proxy_host_label, 9, 0)
        grid.addWidget(self.proxy_port_label, 9, 1)
        grid.addWidget(self.proxy_host, 10, 0)
        grid.addWidget(self.proxy_port, 10, 1)
        grid.addWidget(self.proxy_username_label, 11, 0)
        grid.addWidget(self.proxy_password_label, 11, 1)
        grid.addWidget(self.proxy_username, 12, 0)
        grid.addWidget(self.proxy_password, 12, 1)
        grid.addWidget(self.proxy_api_url_label, 13, 0, 1, 2)
        grid.addWidget(self.proxy_api_url, 14, 0, 1, 2)
        grid.addWidget(self.proxy_api_key_label, 15, 0, 1, 2)
        grid.addWidget(self.proxy_api_key, 16, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        ip_row = QHBoxLayout()
        self.public_ip_status = QLabel("当前出口公网 IP：测试时自动检测")
        self.public_ip_status.setObjectName("ipBadge")
        self.public_ip_status.setWordWrap(True)
        self.copy_public_ip_btn = QPushButton("复制 IP")
        self.copy_public_ip_btn.setObjectName("miniButton")
        self.copy_public_ip_btn.setEnabled(False)
        self.copy_public_ip_btn.clicked.connect(self.copy_public_ip)
        ip_row.addWidget(self.public_ip_status, 1)
        ip_row.addWidget(self.copy_public_ip_btn)
        layout.addLayout(ip_row)

        action_row = QHBoxLayout()
        self.proxy_status = QLabel("当前使用直连")
        self.proxy_status.setObjectName("inlineNote")
        self.proxy_status.setWordWrap(True)
        self.proxy_test_btn = QPushButton("提取并测试")
        self.proxy_test_btn.setObjectName("secondaryButton")
        self.proxy_test_btn.clicked.connect(self.test_proxy)
        action_row.addWidget(self.proxy_status, 1)
        action_row.addWidget(self.proxy_test_btn)
        layout.addLayout(action_row)

        for field in (
            self.proxy_type,
            self.proxy_import,
            self.proxy_host,
            self.proxy_port,
            self.proxy_username,
            self.proxy_password,
            self.proxy_api_url,
            self.proxy_api_key,
        ):
            if isinstance(field, QLineEdit):
                field.textChanged.connect(self._proxy_fields_changed)
            else:
                field.currentIndexChanged.connect(self._proxy_fields_changed)
        self.proxy_pool.textChanged.connect(self._proxy_fields_changed)
        self.proxy_pool_uses_min.valueChanged.connect(
            self._proxy_fields_changed
        )
        self.proxy_pool_uses_max.valueChanged.connect(
            self._proxy_fields_changed
        )
        return card

    def _registration_card(self) -> QFrame:
        card, layout = self._card(
            "固定申请资料",
            "联系电话按起止区间逐单递增；固定地址后自动追加本单尾号。",
        )
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)

        self.last_name = QLineEdit()
        self.first_name = QLineEdit()
        self.contact_phone = QLineEdit()
        self.contact_phone.setPlaceholderText("例如 13800000000")
        self.contact_phone.setMaxLength(11)
        self.contact_phone_end = QLineEdit()
        self.contact_phone_end.setPlaceholderText("留空则固定；例如 13800000999")
        self.contact_phone_end.setMaxLength(11)
        self.chinese_address = QLineEdit()
        self.chinese_address.setPlaceholderText(
            "填写固定部分，程序会在末尾继续追加 1、2、3……"
        )
        self.address_suffix_start = QSpinBox()
        self.address_suffix_start.setRange(1, 1_000_000)
        self.address_suffix_end = QSpinBox()
        self.address_suffix_end.setRange(1, 1_000_000)
        self.referral_code = QLineEdit()
        self.freecard_referrer = QLineEdit()
        self.coupon_code = QLineEdit()
        self.expected_price = QLineEdit()
        self.purchase_route = QComboBox()
        self.purchase_route.addItem(
            "预存 £1 领卡",
            PURCHASE_ROUTE_FREECARD,
        )
        self.purchase_route.addItem(
            "50GB · £11.9/30天（优惠后 £5.95）",
            PURCHASE_ROUTE_50GB,
        )
        self.browser_channel = QComboBox()
        self.browser_channel.addItems(["msedge", "chrome", "chromium"])

        grid.addWidget(self._field_label("本次申请路线"), 0, 0, 1, 2)
        grid.addWidget(self.purchase_route, 1, 0, 1, 2)
        grid.addWidget(self._field_label("姓"), 2, 0)
        grid.addWidget(self._field_label("名"), 2, 1)
        grid.addWidget(self.last_name, 3, 0)
        grid.addWidget(self.first_name, 3, 1)
        grid.addWidget(self._field_label("联系电话起始号码"), 4, 0)
        grid.addWidget(self._field_label("联系电话结束号码"), 4, 1)
        grid.addWidget(self.contact_phone, 5, 0)
        grid.addWidget(self.contact_phone_end, 5, 1)
        grid.addWidget(self._field_label("固定中国收货地址"), 6, 0, 1, 2)
        grid.addWidget(self.chinese_address, 7, 0, 1, 2)
        grid.addWidget(self._field_label("地址尾号起始数字"), 8, 0)
        grid.addWidget(self._field_label("地址尾号结束数字"), 8, 1)
        grid.addWidget(self.address_suffix_start, 9, 0)
        grid.addWidget(self.address_suffix_end, 9, 1)
        grid.addWidget(self._field_label("浏览器"), 10, 0)
        grid.addWidget(self._field_label("£1 路线推荐人号码"), 10, 1)
        grid.addWidget(self.browser_channel, 11, 0)
        grid.addWidget(self.freecard_referrer, 11, 1)
        grid.addWidget(self._field_label("50GB 路线推荐码"), 12, 0)
        grid.addWidget(self._field_label("50GB 路线优惠码"), 12, 1)
        grid.addWidget(self.referral_code, 13, 0)
        grid.addWidget(self.coupon_code, 13, 1)
        grid.addWidget(self._field_label("50GB 优惠后金额（GBP）"), 14, 0, 1, 2)
        grid.addWidget(self.expected_price, 15, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
        return card

    def _workflow_card(self) -> QFrame:
        card, layout = self._card(
            "申请流程",
            "关键节点清晰分离；微信支付仍由你本人扫码确认。",
        )

        steps = QHBoxLayout()
        steps.setSpacing(8)
        steps.addWidget(self._step("01", "服务器建档", "生成客户与专属邮箱"))
        steps.addWidget(self._step("02", "自动填写", "套餐、验证码与地址"))
        steps.addWidget(self._step("03", "支付完成", "保存订单号与付款金额"))
        layout.addLayout(steps)

        batch_panel = QFrame()
        batch_panel.setObjectName("batchPanel")
        batch_layout = QHBoxLayout(batch_panel)
        batch_layout.setContentsMargins(14, 11, 14, 11)
        batch_layout.setSpacing(10)
        self.continuous_enabled = QCheckBox("连续申请")
        self.continuous_enabled.setObjectName("continuousToggle")
        self.continuous_enabled.toggled.connect(
            self._update_continuous_controls
        )
        self.continuous_count_label = self._field_label("目标数量")
        self.continuous_count = QSpinBox()
        self.continuous_count.setRange(1, 1000)
        self.continuous_count.setSuffix(" 张")
        self.continuous_count.setFixedWidth(110)
        self.continuous_count.valueChanged.connect(
            self._update_continuous_controls
        )
        self.continuous_workers_label = self._field_label("并发线程")
        self.continuous_workers = QSpinBox()
        self.continuous_workers.setRange(1, 10)
        self.continuous_workers.setSuffix(" 个")
        self.continuous_workers.setFixedWidth(90)
        self.continuous_workers.valueChanged.connect(
            self._update_continuous_controls
        )
        self.batch_status = QLabel("单次申请")
        self.batch_status.setObjectName("batchStatus")
        batch_layout.addWidget(self.continuous_enabled)
        batch_layout.addSpacing(8)
        batch_layout.addWidget(self.continuous_count_label)
        batch_layout.addWidget(self.continuous_count)
        batch_layout.addSpacing(8)
        batch_layout.addWidget(self.continuous_workers_label)
        batch_layout.addWidget(self.continuous_workers)
        batch_layout.addStretch(1)
        batch_layout.addWidget(self.batch_status)
        layout.addWidget(batch_panel)

        self.batch_progress = QProgressBar()
        self.batch_progress.setObjectName("batchProgress")
        self.batch_progress.setRange(0, 1)
        self.batch_progress.setValue(0)
        self.batch_progress.setFormat("本轮 %v / %m")
        self.batch_progress.setTextVisible(True)
        layout.addWidget(self.batch_progress)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.start_btn = QPushButton("开始 / 继续申请")
        self.start_btn.setObjectName("primaryButton")
        self.start_btn.clicked.connect(self.start_automation)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("dangerButton")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_automation)
        self.save_btn = QPushButton("保存设置")
        self.save_btn.setObjectName("quietButton")
        self.save_btn.clicked.connect(self.save_settings)
        actions.addWidget(self.start_btn, 1)
        actions.addWidget(self.stop_btn)
        actions.addWidget(self.save_btn)
        layout.addLayout(actions)

        stage_row = QHBoxLayout()
        self.stage_label = QLabel("等待开始")
        self.stage_label.setObjectName("stageLabel")
        self.stage_counter = QLabel(f"0 / {len(self.STAGES)}")
        self.stage_counter.setObjectName("stageCounter")
        stage_row.addWidget(self.stage_label)
        stage_row.addStretch(1)
        stage_row.addWidget(self.stage_counter)
        layout.addLayout(stage_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, len(self.STAGES))
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)

        customer_panel = QFrame()
        customer_panel.setObjectName("customerPanel")
        customer_layout = QGridLayout(customer_panel)
        customer_layout.setContentsMargins(14, 12, 14, 12)
        customer_layout.setHorizontalSpacing(10)
        customer_layout.addWidget(self._field_label("当前客户"), 0, 0)
        self.customer_label = QLabel("尚未创建")
        self.customer_label.setObjectName("customerValue")
        self.customer_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        customer_layout.addWidget(self.customer_label, 1, 0)
        self.copy_email_btn = QPushButton("复制邮箱")
        self.copy_email_btn.setObjectName("miniButton")
        self.copy_email_btn.setEnabled(False)
        self.copy_email_btn.clicked.connect(self.copy_current_email)
        customer_layout.addWidget(self.copy_email_btn, 0, 1, 2, 1)
        customer_layout.setColumnStretch(0, 1)
        layout.addWidget(customer_panel)

        note = QLabel(
            "支付成功页出现后立即完成本单；手机号不参与流程判定，"
            "订单确认邮件由服务器后台继续识别。"
        )
        note.setObjectName("inlineNote")
        note.setWordWrap(True)
        layout.addWidget(note)
        return card

    def _log_card(self) -> QFrame:
        card, layout = self._card("运行记录")
        self.log_box = QPlainTextEdit()
        self.log_box.setObjectName("logBox")
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(170)
        self.log_box.setPlaceholderText("连接和申请步骤会显示在这里。")
        layout.addWidget(self.log_box)
        return card

    def _load_config(self) -> None:
        config = self.config
        self.server_url.setText(config.server_url)
        self.app_password.setText(config.app_password)
        self.remember_credentials.setChecked(config.remember_credentials)
        self.last_name.setText(config.registration.last_name)
        self.first_name.setText(config.registration.first_name)
        self.contact_phone.setText(config.registration.contact_phone)
        self.contact_phone_end.setText(config.registration.contact_phone_end)
        self.chinese_address.setText(config.registration.chinese_address)
        self.address_suffix_start.setValue(
            config.registration.address_suffix_start
        )
        self.address_suffix_end.setValue(
            config.registration.address_suffix_end
        )
        self.referral_code.setText(config.registration.referral_code)
        self.freecard_referrer.setText(config.registration.freecard_referrer)
        self.coupon_code.setText(config.registration.coupon_code)
        self.expected_price.setText(config.registration.expected_price_gbp)
        route_index = self.purchase_route.findData(config.purchase_route)
        self.purchase_route.setCurrentIndex(max(0, route_index))
        self.continuous_enabled.setChecked(config.continuous_enabled)
        self.continuous_count.setValue(config.continuous_count)
        self.continuous_workers.setValue(config.continuous_workers)
        self._update_continuous_controls()
        index = self.browser_channel.findText(config.browser_channel)
        self.browser_channel.setCurrentIndex(max(0, index))
        mode_index = self.proxy_mode.findData(config.proxy.mode)
        self.proxy_mode.setCurrentIndex(max(0, mode_index))
        type_index = self.proxy_type.findData(config.proxy.proxy_type)
        self.proxy_type.setCurrentIndex(max(0, type_index))
        self.proxy_host.setText(config.proxy.host)
        self.proxy_port.setText(config.proxy.port)
        self.proxy_username.setText(config.proxy.username)
        self.proxy_password.setText(config.proxy.password)
        self.proxy_api_url.setText(config.proxy.api_url)
        self.proxy_api_key.setText(config.proxy.api_key)
        self.proxy_api_url.setCursorPosition(0)
        self.proxy_api_url.setToolTip(config.proxy.api_url)
        self.proxy_pool.setPlainText(config.proxy.pool)
        self.proxy_pool_uses_min.setValue(config.proxy.pool_uses_min)
        self.proxy_pool_uses_max.setValue(config.proxy.pool_uses_max)
        self.proxy_import.clear()
        self.telegram_enabled.setChecked(config.telegram.enabled)
        self.telegram_bot_token.setText(config.telegram.bot_token)
        self.telegram_chat_id.setText(config.telegram.chat_id)
        self._update_proxy_fields()
        self._update_telegram_fields()

    def collect_config(self) -> AppConfig:
        registration = RegistrationDefaults(
            last_name=self.last_name.text().strip(),
            first_name=self.first_name.text().strip(),
            contact_phone=self.contact_phone.text().strip(),
            contact_phone_end=self.contact_phone_end.text().strip(),
            chinese_address=self.chinese_address.text().strip(),
            address_suffix_start=self.address_suffix_start.value(),
            address_suffix_end=self.address_suffix_end.value(),
            referral_code=self.referral_code.text().strip() or "NTKWJX",
            freecard_referrer=(
                self.freecard_referrer.text().strip()
                or "447942946765"
            ),
            coupon_code=self.coupon_code.text().strip() or "DEAL50OFF",
            expected_price_gbp=self.expected_price.text().strip() or "5.95",
        )
        proxy = ProxyConfig(
            mode=str(self.proxy_mode.currentData() or "none"),
            proxy_type=str(self.proxy_type.currentData() or "socks5"),
            pool=self.proxy_pool.toPlainText().strip(),
            pool_uses_min=self.proxy_pool_uses_min.value(),
            pool_uses_max=self.proxy_pool_uses_max.value(),
            host=self.proxy_host.text().strip(),
            port=self.proxy_port.text().strip(),
            username=self.proxy_username.text().strip(),
            password=self.proxy_password.text(),
            api_url=self.proxy_api_url.text().strip(),
            api_key=self.proxy_api_key.text().strip(),
            api_timeout_seconds=self.config.proxy.api_timeout_seconds,
        )
        telegram = TelegramConfig(
            enabled=self.telegram_enabled.isChecked(),
            bot_token=self.telegram_bot_token.text().strip(),
            chat_id=self.telegram_chat_id.text().strip(),
        )
        return replace(
            self.config,
            server_url=self.server_url.text().strip(),
            app_password=self.app_password.text(),
            remember_credentials=self.remember_credentials.isChecked(),
            purchase_route=str(
                self.purchase_route.currentData()
                or PURCHASE_ROUTE_FREECARD
            ),
            continuous_enabled=self.continuous_enabled.isChecked(),
            continuous_count=self.continuous_count.value(),
            continuous_workers=self.continuous_workers.value(),
            browser_channel=self.browser_channel.currentText(),
            proxy=proxy,
            telegram=telegram,
            registration=registration,
        )

    def _update_continuous_controls(self, *_args: object) -> None:
        enabled = self.continuous_enabled.isChecked()
        self.continuous_count_label.setEnabled(enabled)
        self.continuous_count.setEnabled(enabled)
        self.continuous_workers_label.setEnabled(enabled)
        self.continuous_workers.setEnabled(enabled)
        target = self.continuous_count.value() if enabled else 1
        workers = min(
            target,
            self.continuous_workers.value() if enabled else 1,
        )
        if not (
            self.automation_worker
            and self.automation_worker.isRunning()
        ):
            self.batch_status.setText(
                f"等待开始 · 共 {target} 单 · {workers} 线程"
                if enabled
                else "单次申请"
            )
            self.batch_progress.setRange(0, target)
            self.batch_progress.setValue(0)
            self.start_btn.setText(
                "开始连续申请" if enabled else "开始 / 继续申请"
            )

    def _update_telegram_fields(self, *_args: object) -> None:
        enabled = self.telegram_enabled.isChecked()
        for widget in (
            self.telegram_bot_token_label,
            self.telegram_bot_token,
            self.telegram_chat_id_label,
            self.telegram_chat_id,
            self.telegram_test_btn,
        ):
            widget.setEnabled(enabled)
        self.telegram_status.setText(
            "付款二维码生成后会自动发送，并附带线程、客户、订单和金额。"
            if enabled
            else "Telegram 推送当前未启用。"
        )

    def _update_proxy_fields(self, *_args: object) -> None:
        mode = str(self.proxy_mode.currentData() or "none")
        custom = mode == "custom"
        pool_mode = mode == "pool"
        api_mode = mode == "api"
        self._sync_proxy_protocol()

        for widget in (self.proxy_type_label, self.proxy_type):
            widget.setVisible(custom or pool_mode or api_mode)
        for widget in (
            self.proxy_import_label,
            self.proxy_import,
            self.proxy_import_btn,
        ):
            widget.setVisible(custom)
        for widget in (
            self.proxy_pool_label,
            self.proxy_pool,
            self.proxy_pool_import_btn,
            self.proxy_pool_uses_min_label,
            self.proxy_pool_uses_min,
            self.proxy_pool_uses_max_label,
            self.proxy_pool_uses_max,
        ):
            widget.setVisible(pool_mode)
        for widget in (
            self.proxy_host_label,
            self.proxy_host,
            self.proxy_port_label,
            self.proxy_port,
        ):
            widget.setVisible(custom)
        for widget in (
            self.proxy_username_label,
            self.proxy_username,
            self.proxy_password_label,
            self.proxy_password,
        ):
            widget.setVisible(custom)
        for widget in (self.proxy_api_url_label, self.proxy_api_url):
            widget.setVisible(api_mode)
        qg_api = api_mode and is_qg_proxy_api_url(self.proxy_api_url.text())
        for widget in (self.proxy_api_key_label, self.proxy_api_key):
            widget.setVisible(qg_api)
        for widget in (self.public_ip_status, self.copy_public_ip_btn):
            widget.setVisible(api_mode)
        self.proxy_test_btn.setEnabled(mode != "none")
        if mode == "none":
            self.proxy_status.setText("当前使用直连")
            self.proxy_test_btn.setText("测试代理")
        elif custom:
            self.proxy_status.setText(
                "粘贴整行代理即可自动拆分；启动前会测试访问 CTExcel"
            )
            self.proxy_test_btn.setText("测试代理")
        elif pool_mode:
            self.proxy_status.setText(
                "可一次粘贴几十个代理；每个节点按设定次数使用后"
                "自动切换到下一个"
            )
            self.proxy_test_btn.setText("测试代理池")
        else:
            if qg_api:
                self.proxy_status.setText(
                    "青果 /get 每单提取 1 个节点；支持 area、area_ex、"
                    "isp、distinct 查询参数"
                )
            else:
                self.proxy_status.setText(
                    "接口返回的 HOST:PORT:USERNAME:PASSWORD 会自动识别，"
                    "无需逐项填写"
                )
            self.proxy_test_btn.setText("提取并测试")

    def _proxy_fields_changed(self, *_args: object) -> None:
        mode = str(self.proxy_mode.currentData() or "none")
        self._sync_proxy_protocol()
        qg_api = mode == "api" and is_qg_proxy_api_url(
            self.proxy_api_url.text()
        )
        for widget in (self.proxy_api_key_label, self.proxy_api_key):
            widget.setVisible(qg_api)
        self.proxy_api_url.setToolTip(self.proxy_api_url.text().strip())
        if mode != "none":
            self.proxy_status.setText("代理配置已修改，请提取并测试")

    def test_telegram(self) -> None:
        if self.telegram_worker and self.telegram_worker.isRunning():
            return
        config = TelegramConfig(
            enabled=True,
            bot_token=self.telegram_bot_token.text().strip(),
            chat_id=self.telegram_chat_id.text().strip(),
        )
        self.telegram_test_btn.setEnabled(False)
        self.telegram_status.setText("正在发送 Telegram 测试消息……")
        proxy_config = self.collect_config().proxy

        def run_test() -> dict:
            prepared = prepare_proxy(proxy_config)
            with TelegramNotifier(
                config,
                proxy=prepared.playwright_proxy,
            ) as notifier:
                return notifier.send_test()

        self.telegram_worker = ApiWorker(run_test)
        self.telegram_worker.succeeded.connect(self._telegram_test_ok)
        self.telegram_worker.failed.connect(self._telegram_test_failed)
        self.telegram_worker.finished.connect(
            lambda: self.telegram_test_btn.setEnabled(
                self.telegram_enabled.isChecked()
            )
        )
        self.telegram_worker.start()

    def _telegram_test_ok(self, _result: object) -> None:
        self.telegram_status.setText("Telegram 测试消息已发送")
        self.log("Telegram Bot 测试推送成功")

    def _telegram_test_failed(self, message: str) -> None:
        self.telegram_status.setText(f"Telegram 测试失败：{message}")
        self.log(f"Telegram 测试失败：{message}")
        self._show_message(
            QMessageBox.Warning,
            "Telegram 测试失败",
            message,
        )

    def _sync_proxy_protocol(self) -> None:
        mode = str(self.proxy_mode.currentData() or "none")
        cliproxy = (
            mode == "api"
            and is_cliproxy_whitelist_url(self.proxy_api_url.text())
        )
        if cliproxy:
            index = self.proxy_type.findData("socks5")
            if index >= 0 and self.proxy_type.currentIndex() != index:
                self.proxy_type.blockSignals(True)
                self.proxy_type.setCurrentIndex(index)
                self.proxy_type.blockSignals(False)
            self.proxy_type.setEnabled(False)
            self.proxy_type.setToolTip(
                "该白名单提取接口会自动按 SOCKS5 使用"
            )
        else:
            self.proxy_type.setEnabled(mode != "none")
            self.proxy_type.setToolTip("")

    def import_proxy_from_clipboard(self) -> None:
        value = QGuiApplication.clipboard().text().strip()
        self.proxy_import.setText(value)
        self._apply_proxy_line(show_error=True)

    def import_proxy_pool_from_clipboard(self) -> None:
        value = QGuiApplication.clipboard().text().strip()
        if not value:
            self._show_message(
                QMessageBox.Warning,
                "代理池",
                "剪贴板中没有代理内容。",
            )
            return
        self.proxy_pool.setPlainText(value)
        count = len([line for line in value.splitlines() if line.strip()])
        self.proxy_status.setText(
            f"已粘贴 {count} 行代理，点击“测试代理池”验证"
        )

    def _apply_proxy_line(self, *, show_error: bool = False) -> None:
        value = self.proxy_import.text().strip()
        if not value:
            if show_error:
                self._show_message(
                    QMessageBox.Warning,
                    "代理格式",
                    "请先复制或粘贴整行代理。\n\n"
                    "支持：hostname:port:username:password",
                )
            return
        try:
            proxy = parse_proxy_payload(
                value,
                default_scheme=str(
                    self.proxy_type.currentData() or "socks5"
                ),
            )
            parsed = urlsplit(proxy["server"])
            if not parsed.hostname or not parsed.port:
                raise ProxyError("代理地址或端口缺失")
        except (ProxyError, ValueError) as exc:
            self.proxy_status.setText(f"整行代理识别失败：{exc}")
            if show_error:
                self._show_message(
                    QMessageBox.Warning,
                    "代理格式错误",
                    f"{exc}\n\n支持："
                    "hostname:port:username:password",
                )
            return

        type_index = self.proxy_type.findData(parsed.scheme)
        if type_index >= 0:
            self.proxy_type.setCurrentIndex(type_index)
        self.proxy_host.setText(parsed.hostname)
        self.proxy_port.setText(str(parsed.port))
        self.proxy_username.setText(str(proxy.get("username") or ""))
        self.proxy_password.setText(str(proxy.get("password") or ""))
        label = masked_proxy_label(proxy)
        self.proxy_import.clear()
        self.proxy_import.setPlaceholderText(
            "已自动拆分，可继续粘贴下一条代理"
        )
        self.proxy_status.setText(f"整行代理已识别：{label}")
        self.log(f"固定代理已从整行内容自动识别：{label}")

    def _set_public_ip(self, value: str = "", error: str = "") -> None:
        self.current_public_ip = value.strip()
        self.copy_public_ip_btn.setEnabled(bool(self.current_public_ip))
        if self.current_public_ip:
            self.public_ip_status.setText(
                f"当前出口公网 IP：{self.current_public_ip}"
            )
        elif error:
            self.public_ip_status.setText(f"公网 IP：{error}")
        else:
            self.public_ip_status.setText(
                "当前出口公网 IP：测试时自动检测"
            )

    def copy_public_ip(self) -> None:
        if self.current_public_ip:
            QGuiApplication.clipboard().setText(self.current_public_ip)
            self.log("当前出口公网 IP 已复制")

    def test_proxy(self) -> None:
        if self.proxy_worker and self.proxy_worker.isRunning():
            return
        config = self.collect_config()
        if config.proxy.mode == "none":
            self.proxy_status.setText("当前使用直连")
            return

        def action() -> dict[str, str]:
            prepared = prepare_proxy(config.proxy)
            return {
                "label": masked_proxy_label(prepared.playwright_proxy),
                "public_ip": prepared.public_ip,
                "public_ip_error": prepared.public_ip_error,
            }

        self.proxy_status.setText("正在提取并验证代理连接……")
        if config.proxy.mode == "api":
            self._set_public_ip(error="检测中……")
        self.proxy_test_btn.setEnabled(False)
        self.proxy_worker = ApiWorker(action)
        self.proxy_worker.succeeded.connect(self._proxy_test_ok)
        self.proxy_worker.failed.connect(self._proxy_test_failed)
        self.proxy_worker.finished.connect(
            lambda: self.proxy_test_btn.setEnabled(True)
        )
        self.proxy_worker.start()

    def _proxy_test_ok(self, result: object) -> None:
        data = result if isinstance(result, dict) else {}
        label = str(data.get("label") or "代理")
        self._set_public_ip(
            str(data.get("public_ip") or ""),
            str(data.get("public_ip_error") or ""),
        )
        self.proxy_status.setText(f"代理提取和目标连接正常：{label}")
        self.log(f"代理测试成功：{label}")

    def _proxy_test_failed(self, message: str) -> None:
        match = re.search(r"当前出口公网 IP：([^\r\n]+)", message)
        if match and "检测失败" not in match.group(1):
            self._set_public_ip(match.group(1).strip())
        else:
            self._set_public_ip(error="检测失败")
        self.proxy_status.setText(f"代理测试失败：{message}")
        self.log(f"代理测试失败：{message}")
        self._show_message(
            QMessageBox.Warning,
            "代理测试失败",
            message,
        )

    def _build_message_box(
        self,
        icon: QMessageBox.Icon,
        title: str,
        message: str,
    ) -> QMessageBox:
        box = QMessageBox(self)
        box.setIcon(icon)
        box.setWindowTitle(title)
        box.setText(message)
        box.setTextFormat(Qt.PlainText)
        box.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
        )
        box.setStandardButtons(QMessageBox.Ok)
        box.setDefaultButton(QMessageBox.Ok)
        box.setStyleSheet(
            """
            QMessageBox {
                background-color: #ffffff;
            }
            QMessageBox QLabel {
                padding: 6px 4px;
                color: #172033;
                background-color: #ffffff;
                font-family: "Microsoft YaHei UI", "Segoe UI";
                font-size: 13px;
            }
            QMessageBox QPushButton {
                min-width: 88px;
                min-height: 36px;
                padding: 0 16px;
                color: #ffffff;
                background-color: #246bfe;
                border: 1px solid #246bfe;
                border-radius: 8px;
                font-weight: 700;
            }
            QMessageBox QPushButton:hover {
                background-color: #1758d8;
            }
            """
        )
        message_label = box.findChild(QLabel, "qt_msgbox_label")
        if message_label:
            message_label.setMinimumWidth(460)
            message_label.setWordWrap(True)
        return box

    def _show_message(
        self,
        icon: QMessageBox.Icon,
        title: str,
        message: str,
    ) -> None:
        self._build_message_box(icon, title, message).exec()

    def _set_connection_state(self, state: str, text: str) -> None:
        self.connection_pill.setProperty("state", state)
        self.connection_pill.setText(text)
        style = self.connection_pill.style()
        style.unpolish(self.connection_pill)
        style.polish(self.connection_pill)

    def _connection_fields_changed(self) -> None:
        if self.connection_pill.property("state") != "pending":
            self._set_connection_state("pending", "●  等待连接")
            self.connection_detail.setText(
                "连接信息已修改，请重新测试连接。"
            )

    def save_settings(self) -> None:
        self.config = self.collect_config()
        save_config(self.config)
        self.log("设置已保存到本机")

    def test_connection(self) -> None:
        if self.api_worker and self.api_worker.isRunning():
            return
        config = self.collect_config()
        if not config.server_url or not config.app_password:
            message = "请填写服务器地址和客户端连接口令"
            self._connection_failed(message)
            return

        def action() -> dict[str, Any]:
            with AdminApi(config.server_url, config.app_password) as api:
                return api.connect()

        self._set_connection_state("working", "●  连接中")
        self.connection_detail.setText("正在验证 CTExcel 客户端 API……")
        self.test_connection_btn.setEnabled(False)
        self.api_worker = ApiWorker(action)
        self.api_worker.succeeded.connect(self._connection_ok)
        self.api_worker.failed.connect(self._connection_failed)
        self.api_worker.finished.connect(
            lambda: self.test_connection_btn.setEnabled(True)
        )
        self.api_worker.start()

    def _connection_ok(self, result: object) -> None:
        data = result if isinstance(result, dict) else {}
        count = int(data.get("ctexcel_customer_count") or 0)
        pending_count = int(data.get("pending_customer_count") or 0)
        self._set_connection_state("ok", "●  已连接")
        self.connection_detail.setText(
            f"服务器连接正常 · API v{data.get('api_version', 1)} · "
            f"现有 CTExcel 客户 {count} 位 · 无手机号 {pending_count} 位"
        )
        self.log(
            f"服务器连接成功，当前 CTExcel 客户 {count} 位，"
            f"其中无手机号 {pending_count} 位"
        )

    def _connection_failed(self, message: str) -> None:
        self._set_connection_state("error", "●  连接失败")
        self.connection_detail.setText(message)
        self.log(f"连接失败：{message}")
        self._show_message(QMessageBox.Warning, "连接失败", message)

    def start_automation(self) -> None:
        if self.automation_worker and self.automation_worker.isRunning():
            return
        self.config = self.collect_config()
        save_config(self.config)
        target = application_target(self.config)
        signature = (
            self.config.continuous_enabled,
            target,
            self.config.purchase_route,
            self.config.continuous_workers,
            self.config.registration.contact_phone,
            self.config.registration.contact_phone_end,
            self.config.registration.chinese_address,
            self.config.registration.address_suffix_start,
            self.config.registration.address_suffix_end,
        )
        resume = (
            self.batch_resume_pending
            and self.batch_signature == signature
            and self.batch_completed_count < target
        )
        if not resume:
            self.batch_completed_count = 0
        self.batch_target_count = target
        self.batch_signature = signature
        self.current_customer = None
        self.customer_label.setText("正在检查待完成客户与专属邮箱……")
        self.copy_email_btn.setEnabled(False)
        self.progress.setValue(0)
        self.stage_counter.setText(f"0 / {len(self.STAGES)}")
        self.batch_progress.setRange(0, target)
        self.batch_progress.setValue(self.batch_completed_count)
        if not resume:
            self.log_box.clear()
            self.log(
                f"开始 CTExcel 申请流程，共 {target} 单；"
                f"并发 {min(target, self.config.continuous_workers)} 线程；"
                "优先检查并继续未生成订单的客户"
            )
        else:
            self.log(
                f"继续连续申请：已完成 {self.batch_completed_count} / "
                f"{target} 单"
            )
        self.automation_worker = AutomationWorker(
            self.config,
            completed_before=self.batch_completed_count,
        )
        self.automation_worker.log_message.connect(self.log)
        self.automation_worker.stage_changed.connect(self.on_stage)
        self.automation_worker.customer_created.connect(self.on_customer_created)
        self.automation_worker.item_started.connect(self.on_item_started)
        self.automation_worker.item_completed.connect(
            self.on_item_completed
        )
        self.automation_worker.succeeded.connect(self.on_success)
        self.automation_worker.failed.connect(self.on_failure)
        self.automation_worker.finished.connect(self.on_finished)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.test_connection_btn.setEnabled(False)
        self.purchase_route.setEnabled(False)
        self.continuous_enabled.setEnabled(False)
        self.continuous_count.setEnabled(False)
        self.continuous_workers.setEnabled(False)
        self.telegram_enabled.setEnabled(False)
        self.telegram_bot_token.setEnabled(False)
        self.telegram_chat_id.setEnabled(False)
        self.telegram_test_btn.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.automation_worker.start()

    def stop_automation(self) -> None:
        if self.automation_worker:
            self.automation_worker.stop()
            self.log("正在停止当前流程……")

    def on_stage(self, stage: str) -> None:
        self.stage_label.setText(stage)
        base_stage = (
            stage.split("：", 1)[1]
            if stage.startswith("线程 ") and "：" in stage
            else stage
        )
        try:
            index = self.STAGES.index(base_stage) + 1
        except ValueError:
            index = self.progress.value()
        self.progress.setValue(index)
        self.stage_counter.setText(f"{index} / {len(self.STAGES)}")
        self.log(f"当前步骤：{stage}")

    def on_customer_created(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        self.current_customer = payload
        self.customer_label.setText(
            f"#{payload.get('customer_id')}  ·  {payload.get('email', '')}"
        )
        self.copy_email_btn.setEnabled(bool(payload.get("email")))

    def on_item_started(self, ordinal: int, total: int) -> None:
        self.batch_target_count = total
        self.batch_status.setText(
            f"已调度第 {ordinal} / {total} 单 · "
            f"已完成 {self.batch_completed_count}"
        )
        self.batch_progress.setRange(0, total)
        self.batch_progress.setValue(self.batch_completed_count)

    def on_item_completed(
        self,
        payload: object,
        ordinal: int,
        total: int,
    ) -> None:
        if not isinstance(payload, AutomationResult):
            return
        self.batch_completed_count = ordinal
        self.batch_target_count = total
        self.batch_progress.setRange(0, total)
        self.batch_progress.setValue(ordinal)
        self.batch_status.setText(
            f"已完成 {ordinal} / {total} 单"
            if ordinal < total
            else f"本轮 {total} 单全部完成"
        )
        self.log(
            f"第 {payload.batch_ordinal or ordinal} 单完成："
            f"客户 #{payload.customer_id} · "
            f"{payload.order_number or '订单号等待同步'}"
        )

    def on_success(self, payload: object) -> None:
        if not isinstance(payload, AutomationBatchResult):
            return
        self.batch_completed_count = payload.completed_count
        self.batch_target_count = payload.total_count
        self.batch_resume_pending = False
        result = payload.last_result
        if result is None:
            return
        self.start_btn.setText(
            "重新开始连续申请"
            if payload.total_count > 1
            else "开始下一位客户"
        )
        expected = (
            "1.00"
            if self.config.purchase_route == PURCHASE_ROUTE_FREECARD
            else self.config.registration.expected_price_gbp
        )
        if payload.total_count > 1:
            summary = (
                f"连续申请已完成：{payload.completed_count} / "
                f"{payload.total_count} 单\n"
                f"最后客户：#{result.customer_id}\n"
                f"最后订单：{result.order_number or '等待邮件同步'}\n"
                "订单邮件将由服务器继续同步"
            )
        else:
            summary = (
                f"客户 #{result.customer_id}\n"
                f"邮箱：{result.email}\n"
                f"订单号：{result.order_number or '等待邮件同步'}\n"
                f"支付：£{result.transaction_amount or expected}"
            )
        self.log(
            f"流程已完成：{payload.completed_count} / "
            f"{payload.total_count} 单；服务器邮件同步会继续运行"
        )
        self._show_message(
            QMessageBox.Information,
            "CTExcel 申请完成",
            summary,
        )

    def on_failure(self, message: str) -> None:
        self.log(f"流程停止：{message}")
        self.batch_resume_pending = (
            self.config.continuous_enabled
            and self.batch_completed_count < self.batch_target_count
        )
        if self.batch_resume_pending:
            next_ordinal = self.batch_completed_count + 1
            self.batch_status.setText(
                f"暂停在第 {next_ordinal} / "
                f"{self.batch_target_count} 单"
            )
            self.start_btn.setText(
                f"重试第 {next_ordinal} 单并继续"
            )
        else:
            self.start_btn.setText("重试当前客户")
        self._show_message(QMessageBox.Warning, "流程未完成", message)

    def on_finished(self) -> None:
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.test_connection_btn.setEnabled(True)
        self.purchase_route.setEnabled(True)
        self.continuous_enabled.setEnabled(True)
        self.continuous_count.setEnabled(
            self.continuous_enabled.isChecked()
        )
        self.continuous_workers.setEnabled(
            self.continuous_enabled.isChecked()
        )
        self.telegram_enabled.setEnabled(True)
        self._update_telegram_fields()
        self.save_btn.setEnabled(True)

    def copy_current_email(self) -> None:
        email = str((self.current_customer or {}).get("email") or "")
        if email:
            QGuiApplication.clipboard().setText(email)
            self.log("邮箱已复制")

    def log(self, message: str) -> None:
        self.log_box.appendPlainText(message)
        scrollbar = self.log_box.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            * {
                font-family: "Microsoft YaHei UI", "Segoe UI";
                font-size: 13px;
                color: #172033;
            }
            QWidget#root, QWidget#scrollContent, QScrollArea#mainScroll {
                background: #f3f6fa;
            }
            QScrollArea#mainScroll { border: 0; }
            QLabel#eyebrow {
                color: #2878ff;
                font-size: 11px;
                font-weight: 700;
                letter-spacing: 1px;
            }
            QLabel#title {
                color: #10233f;
                font-size: 28px;
                font-weight: 800;
            }
            QLabel#subtitle {
                color: #64748b;
                font-size: 13px;
            }
            QLabel#connectionPill {
                border-radius: 16px;
                padding: 7px 13px;
                font-weight: 700;
            }
            QLabel#connectionPill[state="pending"] {
                color: #64748b;
                background: #e8edf4;
            }
            QLabel#connectionPill[state="working"] {
                color: #8a5800;
                background: #fff1c7;
            }
            QLabel#connectionPill[state="ok"] {
                color: #08704d;
                background: #dff7ed;
            }
            QLabel#connectionPill[state="error"] {
                color: #b42318;
                background: #fee4e2;
            }
            QFrame#card {
                background: #ffffff;
                border: 1px solid #e1e7ef;
                border-radius: 14px;
            }
            QLabel#cardTitle {
                color: #172033;
                font-size: 17px;
                font-weight: 750;
            }
            QLabel#cardSubtitle, QLabel#inlineNote {
                color: #68778d;
                font-size: 12px;
                line-height: 1.35;
            }
            QLabel#fieldLabel {
                color: #526174;
                font-size: 11px;
                font-weight: 700;
            }
            QLabel#ipBadge {
                padding: 9px 11px;
                color: #124e3b;
                background: #e7f8f1;
                border: 1px solid #c4eadc;
                border-radius: 8px;
                font-size: 12px;
                font-weight: 700;
            }
            QLineEdit, QComboBox, QSpinBox {
                min-height: 36px;
                padding: 0 10px;
                color: #14213d;
                background: #f9fbfd;
                border: 1px solid #d7dee8;
                border-radius: 8px;
                selection-background-color: #2878ff;
            }
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
                background: #ffffff;
                border: 1px solid #2878ff;
            }
            QCheckBox {
                color: #526174;
                spacing: 7px;
            }
            QPushButton {
                min-height: 36px;
                padding: 0 14px;
                border-radius: 8px;
                font-weight: 700;
            }
            QPushButton#primaryButton {
                color: #ffffff;
                background: #246bfe;
                border: 1px solid #246bfe;
            }
            QPushButton#primaryButton:hover { background: #1758d8; }
            QPushButton#secondaryButton {
                color: #1758d8;
                background: #edf4ff;
                border: 1px solid #cfe0ff;
            }
            QPushButton#quietButton, QPushButton#miniButton {
                color: #344054;
                background: #ffffff;
                border: 1px solid #d6dde7;
            }
            QPushButton#dangerButton {
                color: #b42318;
                background: #fff3f2;
                border: 1px solid #ffd5d2;
            }
            QPushButton:disabled {
                color: #9ba7b7;
                background: #eef1f5;
                border-color: #e2e7ee;
            }
            QFrame#stepCard {
                background: #f7f9fc;
                border: 1px solid #e5eaf1;
                border-radius: 10px;
            }
            QLabel#stepNumber {
                color: #2878ff;
                font-size: 11px;
                font-weight: 800;
            }
            QLabel#stepTitle {
                color: #23324a;
                font-size: 13px;
                font-weight: 750;
            }
            QLabel#stepDetail {
                color: #78869a;
                font-size: 10px;
            }
            QLabel#stageLabel {
                color: #1758d8;
                font-size: 14px;
                font-weight: 750;
            }
            QLabel#stageCounter {
                color: #77869a;
                font-size: 11px;
            }
            QProgressBar {
                min-height: 7px;
                max-height: 7px;
                background: #e7ecf3;
                border: 0;
                border-radius: 3px;
            }
            QProgressBar::chunk {
                background: #2878ff;
                border-radius: 3px;
            }
            QFrame#customerPanel {
                background: #f4f8ff;
                border: 1px solid #d9e6ff;
                border-radius: 10px;
            }
            QFrame#batchPanel {
                background: #f7f9fc;
                border: 1px solid #e2e8f1;
                border-radius: 10px;
            }
            QCheckBox#continuousToggle {
                color: #16345f;
                font-size: 13px;
                font-weight: 750;
            }
            QLabel#batchStatus {
                color: #1758d8;
                font-size: 12px;
                font-weight: 750;
            }
            QProgressBar#batchProgress {
                min-height: 22px;
                max-height: 22px;
                color: #16345f;
                background: #eaf0f8;
                border: 0;
                border-radius: 7px;
                text-align: center;
                font-size: 11px;
                font-weight: 700;
            }
            QProgressBar#batchProgress::chunk {
                background: #9fc2ff;
                border-radius: 7px;
            }
            QLabel#customerValue {
                color: #16345f;
                font-size: 13px;
                font-weight: 700;
            }
            QPlainTextEdit#logBox {
                min-height: 170px;
                padding: 12px;
                color: #d6e2f2;
                background: #152238;
                border: 0;
                border-radius: 10px;
                font-family: "Cascadia Mono", "Consolas";
                font-size: 11px;
                selection-background-color: #2f6edb;
            }
            QMessageBox {
                background-color: #ffffff;
            }
            QMessageBox QLabel {
                color: #172033;
                background-color: #ffffff;
            }
            QScrollBar:vertical {
                width: 9px;
                background: transparent;
                margin: 2px;
            }
            QScrollBar::handle:vertical {
                min-height: 28px;
                background: #c6cfdb;
                border-radius: 4px;
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0;
            }
            """
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.automation_worker and self.automation_worker.isRunning():
            self.automation_worker.stop()
            self.automation_worker.wait(5000)
        event.accept()


def main() -> None:
    app = QApplication([])
    app.setApplicationName("CTExcelApplyClient")
    window = MainWindow()
    window.show()
    app.exec()
