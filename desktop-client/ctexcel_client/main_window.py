from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Optional

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
    QVBoxLayout,
    QWidget,
)

from .api import AdminApi
from .automation import AutomationResult, CTExcelAutomation
from .config import AppConfig, RegistrationDefaults, load_config, save_config


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
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.session: Optional[CTExcelAutomation] = None

    def stop(self) -> None:
        if self.session:
            self.session.stop()

    def run(self) -> None:
        try:
            self.session = CTExcelAutomation(
                self.config,
                log=self.log_message.emit,
                stage=self.stage_changed.emit,
                customer_created=self.customer_created.emit,
            )
            self.succeeded.emit(self.session.run())
        except Exception as exc:  # noqa: BLE001 - thread boundary
            self.failed.emit(str(exc))
        finally:
            self.session = None


class MainWindow(QMainWindow):
    STAGES = [
        "连接客户管理",
        "准备 CTExcel 客户",
        "启动浏览器",
        "选择 50GB 套餐",
        "配置实体卡",
        "填写客户资料",
        "应用半价优惠",
        "确认支付条款",
        "等待人工微信支付",
        "支付成功",
        "同步号码资料",
    ]

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.api_worker: Optional[ApiWorker] = None
        self.automation_worker: Optional[AutomationWorker] = None
        self.current_customer: Optional[dict[str, Any]] = None
        self.setWindowTitle("CTExcel 申请工作台")
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

        self.remember_credentials = QCheckBox("使用 Windows 加密保存连接口令")
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

    def _registration_card(self) -> QFrame:
        card, layout = self._card(
            "固定申请资料",
            "这些信息只保存在本机；每位客户的注册邮箱由服务器单独生成。",
        )
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)

        self.last_name = QLineEdit()
        self.first_name = QLineEdit()
        self.contact_phone = QLineEdit()
        self.chinese_address = QLineEdit()
        self.referral_code = QLineEdit()
        self.coupon_code = QLineEdit()
        self.expected_price = QLineEdit()
        self.browser_channel = QComboBox()
        self.browser_channel.addItems(["msedge", "chrome", "chromium"])

        grid.addWidget(self._field_label("姓"), 0, 0)
        grid.addWidget(self._field_label("名"), 0, 1)
        grid.addWidget(self.last_name, 1, 0)
        grid.addWidget(self.first_name, 1, 1)
        grid.addWidget(self._field_label("固定联系电话"), 2, 0)
        grid.addWidget(self._field_label("浏览器"), 2, 1)
        grid.addWidget(self.contact_phone, 3, 0)
        grid.addWidget(self.browser_channel, 3, 1)
        grid.addWidget(self._field_label("固定中国收货地址"), 4, 0, 1, 2)
        grid.addWidget(self.chinese_address, 5, 0, 1, 2)
        grid.addWidget(self._field_label("推荐码"), 6, 0)
        grid.addWidget(self._field_label("优惠码"), 6, 1)
        grid.addWidget(self.referral_code, 7, 0)
        grid.addWidget(self.coupon_code, 7, 1)
        grid.addWidget(self._field_label("优惠后金额（GBP）"), 8, 0)
        grid.addWidget(self.expected_price, 9, 0)
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
        steps.addWidget(self._step("03", "邮件归档", "同步订单号与手机号"))
        layout.addLayout(steps)

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
            "支付成功即完成客户端流程。订单号、手机号、金额和推荐信息由服务器后台从专属邮箱自动写入。"
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
        self.chinese_address.setText(config.registration.chinese_address)
        self.referral_code.setText(config.registration.referral_code)
        self.coupon_code.setText(config.registration.coupon_code)
        self.expected_price.setText(config.registration.expected_price_gbp)
        index = self.browser_channel.findText(config.browser_channel)
        self.browser_channel.setCurrentIndex(max(0, index))

    def collect_config(self) -> AppConfig:
        registration = RegistrationDefaults(
            last_name=self.last_name.text().strip(),
            first_name=self.first_name.text().strip(),
            contact_phone=self.contact_phone.text().strip(),
            chinese_address=self.chinese_address.text().strip(),
            referral_code=self.referral_code.text().strip() or "NTKWJX",
            coupon_code=self.coupon_code.text().strip() or "DEAL50OFF",
            expected_price_gbp=self.expected_price.text().strip() or "5.95",
        )
        return replace(
            self.config,
            server_url=self.server_url.text().strip(),
            app_password=self.app_password.text(),
            remember_credentials=self.remember_credentials.isChecked(),
            browser_channel=self.browser_channel.currentText(),
            registration=registration,
        )

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
        QMessageBox.warning(self, "连接失败", message)

    def start_automation(self) -> None:
        if self.automation_worker and self.automation_worker.isRunning():
            return
        self.config = self.collect_config()
        save_config(self.config)
        self.current_customer = None
        self.customer_label.setText("正在检查待完成客户与专属邮箱……")
        self.copy_email_btn.setEnabled(False)
        self.progress.setValue(0)
        self.stage_counter.setText(f"0 / {len(self.STAGES)}")
        self.log_box.clear()
        self.log("开始 CTExcel 申请流程；优先检查并继续无手机号客户")
        self.automation_worker = AutomationWorker(self.config)
        self.automation_worker.log_message.connect(self.log)
        self.automation_worker.stage_changed.connect(self.on_stage)
        self.automation_worker.customer_created.connect(self.on_customer_created)
        self.automation_worker.succeeded.connect(self.on_success)
        self.automation_worker.failed.connect(self.on_failure)
        self.automation_worker.finished.connect(self.on_finished)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.test_connection_btn.setEnabled(False)
        self.automation_worker.start()

    def stop_automation(self) -> None:
        if self.automation_worker:
            self.automation_worker.stop()
            self.log("正在停止当前流程……")

    def on_stage(self, stage: str) -> None:
        self.stage_label.setText(stage)
        try:
            index = self.STAGES.index(stage) + 1
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

    def on_success(self, payload: object) -> None:
        if not isinstance(payload, AutomationResult):
            return
        self.start_btn.setText("开始下一位客户")
        summary = (
            f"客户 #{payload.customer_id}\n"
            f"邮箱：{payload.email}\n"
            f"订单号：{payload.order_number or '等待邮件同步'}\n"
            f"手机号：{payload.phone_number or '等待邮件同步'}\n"
            f"支付：£{payload.transaction_amount or self.config.registration.expected_price_gbp}"
        )
        self.log("流程已完成；服务器邮件同步会继续运行")
        QMessageBox.information(self, "CTExcel 申请完成", summary)

    def on_failure(self, message: str) -> None:
        self.log(f"流程停止：{message}")
        self.start_btn.setText("重试当前客户")
        QMessageBox.warning(self, "流程未完成", message)

    def on_finished(self) -> None:
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.test_connection_btn.setEnabled(True)

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
            QLineEdit, QComboBox {
                min-height: 36px;
                padding: 0 10px;
                color: #14213d;
                background: #f9fbfd;
                border: 1px solid #d7dee8;
                border-radius: 8px;
                selection-background-color: #2878ff;
            }
            QLineEdit:focus, QComboBox:focus {
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
