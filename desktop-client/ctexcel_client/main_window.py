from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QCloseEvent, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
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
            result = self.session.run()
            self.succeeded.emit(result)
        except Exception as exc:  # noqa: BLE001 - thread boundary
            self.failed.emit(str(exc))
        finally:
            self.session = None


class MainWindow(QMainWindow):
    STAGES = [
        "连接客户管理",
        "新建 CTExcel 客户",
        "启动浏览器",
        "选择 50GB 套餐",
        "配置实体卡",
        "填写客户资料",
        "应用半价优惠",
        "确认支付条款",
        "等待人工微信支付",
        "支付成功",
    ]

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.api_worker: Optional[ApiWorker] = None
        self.automation_worker: Optional[AutomationWorker] = None
        self.current_customer: Optional[dict[str, Any]] = None
        self.setWindowTitle("CTExcel 自动申请客户端")
        self.resize(980, 780)
        self._build_ui()
        self._load_config()
        self._apply_style()

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(18, 18, 18, 18)
        root_layout.setSpacing(12)

        title = QLabel("CTExcel 自动申请")
        title.setObjectName("title")
        subtitle = QLabel(
            "先在客户管理中新建 CTExcel 客户并取得专属邮箱，再自动完成注册、优惠与微信支付流程。"
        )
        subtitle.setWordWrap(True)
        subtitle.setObjectName("subtitle")
        root_layout.addWidget(title)
        root_layout.addWidget(subtitle)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setSpacing(12)
        content_layout.addWidget(self._connection_group())
        content_layout.addWidget(self._registration_group())
        content_layout.addWidget(self._workflow_group())
        content_layout.addWidget(self._log_group(), 1)
        scroll.setWidget(content)
        root_layout.addWidget(scroll, 1)
        self.setCentralWidget(root)

    def _connection_group(self) -> QGroupBox:
        group = QGroupBox("客户管理连接")
        layout = QGridLayout(group)
        self.server_url = QLineEdit()
        self.server_url.setPlaceholderText("https://后台域名")
        self.entry_path = QLineEdit()
        self.entry_path.setEchoMode(QLineEdit.Password)
        self.entry_path.setPlaceholderText("/随机隐藏入口")
        self.app_password = QLineEdit()
        self.app_password.setEchoMode(QLineEdit.Password)
        self.app_password.setPlaceholderText("APP_PASSWORD")
        self.remember_credentials = QCheckBox("使用 Windows 加密保存入口和口令")
        self.test_connection_btn = QPushButton("测试连接")
        self.test_connection_btn.clicked.connect(self.test_connection)

        layout.addWidget(QLabel("后台地址"), 0, 0)
        layout.addWidget(self.server_url, 0, 1, 1, 3)
        layout.addWidget(QLabel("隐藏入口"), 1, 0)
        layout.addWidget(self.entry_path, 1, 1)
        layout.addWidget(QLabel("访问口令"), 1, 2)
        layout.addWidget(self.app_password, 1, 3)
        layout.addWidget(self.remember_credentials, 2, 1, 1, 2)
        layout.addWidget(self.test_connection_btn, 2, 3)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)
        return group

    def _registration_group(self) -> QGroupBox:
        group = QGroupBox("固定注册资料")
        layout = QGridLayout(group)
        self.last_name = QLineEdit()
        self.first_name = QLineEdit()
        self.contact_phone = QLineEdit()
        self.chinese_address = QLineEdit()
        self.referral_code = QLineEdit()
        self.coupon_code = QLineEdit()
        self.expected_price = QLineEdit()
        self.browser_channel = QComboBox()
        self.browser_channel.addItems(["msedge", "chrome", "chromium"])

        layout.addWidget(QLabel("姓"), 0, 0)
        layout.addWidget(self.last_name, 0, 1)
        layout.addWidget(QLabel("名"), 0, 2)
        layout.addWidget(self.first_name, 0, 3)
        layout.addWidget(QLabel("联系电话"), 1, 0)
        layout.addWidget(self.contact_phone, 1, 1)
        layout.addWidget(QLabel("浏览器"), 1, 2)
        layout.addWidget(self.browser_channel, 1, 3)
        layout.addWidget(QLabel("固定中国地址"), 2, 0)
        layout.addWidget(self.chinese_address, 2, 1, 1, 3)
        layout.addWidget(QLabel("推荐码"), 3, 0)
        layout.addWidget(self.referral_code, 3, 1)
        layout.addWidget(QLabel("优惠码"), 3, 2)
        layout.addWidget(self.coupon_code, 3, 3)
        layout.addWidget(QLabel("半价校验（GBP）"), 4, 0)
        layout.addWidget(self.expected_price, 4, 1)
        hint = QLabel(
            "固定资料只保存在当前 Windows 用户目录；客户邮箱由后台新建客户时自动生成。"
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint, 4, 2, 1, 2)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)
        return group

    def _workflow_group(self) -> QGroupBox:
        group = QGroupBox("申请流程")
        layout = QVBoxLayout(group)

        header = QHBoxLayout()
        self.start_btn = QPushButton("新建 CTExcel 客户并开始申请")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self.start_automation)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_automation)
        self.save_btn = QPushButton("保存设置")
        self.save_btn.clicked.connect(self.save_settings)
        header.addWidget(self.start_btn)
        header.addWidget(self.stop_btn)
        header.addStretch(1)
        header.addWidget(self.save_btn)
        layout.addLayout(header)

        self.stage_label = QLabel("等待开始")
        self.stage_label.setObjectName("stage")
        self.progress = QProgressBar()
        self.progress.setRange(0, len(self.STAGES))
        self.progress.setValue(0)
        layout.addWidget(self.stage_label)
        layout.addWidget(self.progress)

        customer_row = QHBoxLayout()
        customer_row.addWidget(QLabel("当前客户"))
        self.customer_label = QLabel("尚未创建")
        self.copy_email_btn = QPushButton("复制邮箱")
        self.copy_email_btn.setEnabled(False)
        self.copy_email_btn.clicked.connect(self.copy_current_email)
        customer_row.addWidget(self.customer_label, 1)
        customer_row.addWidget(self.copy_email_btn)
        layout.addLayout(customer_row)

        note = QLabel(
            "支付成功后客户端流程结束。客户管理后台按专属邮箱自动扫描 CTExcel 订单邮件，"
            "同步订单号、手机号码、交易金额和推荐信息。"
        )
        note.setWordWrap(True)
        note.setObjectName("hint")
        layout.addWidget(note)
        return group

    def _log_group(self) -> QGroupBox:
        group = QGroupBox("运行日志")
        layout = QVBoxLayout(group)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(190)
        layout.addWidget(self.log_box)
        return group

    def _load_config(self) -> None:
        c = self.config
        self.server_url.setText(c.server_url)
        self.entry_path.setText(c.admin_entry_path)
        self.app_password.setText(c.app_password)
        self.remember_credentials.setChecked(c.remember_credentials)
        self.last_name.setText(c.registration.last_name)
        self.first_name.setText(c.registration.first_name)
        self.contact_phone.setText(c.registration.contact_phone)
        self.chinese_address.setText(c.registration.chinese_address)
        self.referral_code.setText(c.registration.referral_code)
        self.coupon_code.setText(c.registration.coupon_code)
        self.expected_price.setText(c.registration.expected_price_gbp)
        index = self.browser_channel.findText(c.browser_channel)
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
            admin_entry_path=self.entry_path.text().strip(),
            app_password=self.app_password.text(),
            remember_credentials=self.remember_credentials.isChecked(),
            browser_channel=self.browser_channel.currentText(),
            registration=registration,
        )

    def save_settings(self) -> None:
        self.config = self.collect_config()
        save_config(self.config)
        self.log("设置已保存")

    def test_connection(self) -> None:
        if self.api_worker and self.api_worker.isRunning():
            return
        config = self.collect_config()

        def action() -> dict[str, Any]:
            with AdminApi(
                config.server_url,
                config.admin_entry_path,
                config.app_password,
            ) as api:
                status = api.connect()
                rows = api.list_ctexcel_customers()
                return {"status": status, "count": len(rows)}

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
        self.log(f"连接成功，当前 CTExcel 客户 {data.get('count', 0)} 位")

    def _connection_failed(self, message: str) -> None:
        self.log(f"连接失败：{message}")
        QMessageBox.warning(self, "连接失败", message)

    def start_automation(self) -> None:
        if self.automation_worker and self.automation_worker.isRunning():
            return
        self.config = self.collect_config()
        save_config(self.config)
        self.current_customer = None
        self.customer_label.setText("正在创建……")
        self.copy_email_btn.setEnabled(False)
        self.progress.setValue(0)
        self.log_box.clear()
        self.log("开始新的 CTExcel 申请流程")
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
        self.log(f"当前步骤：{stage}")

    def on_customer_created(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        self.current_customer = payload
        self.customer_label.setText(
            f"#{payload.get('customer_id')} · {payload.get('email', '')}"
        )
        self.copy_email_btn.setEnabled(bool(payload.get("email")))

    def on_success(self, payload: object) -> None:
        if not isinstance(payload, AutomationResult):
            return
        summary = (
            f"客户 #{payload.customer_id}\n"
            f"邮箱：{payload.email}\n"
            f"订单号：{payload.order_number or '等待邮件同步'}\n"
            f"手机号：{payload.phone_number or '等待邮件同步'}\n"
            f"支付：£{payload.transaction_amount or self.config.registration.expected_price_gbp}"
        )
        self.log("流程已完成；后台邮件同步会继续运行")
        QMessageBox.information(self, "CTExcel 申请完成", summary)

    def on_failure(self, message: str) -> None:
        self.log(f"流程停止：{message}")
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
            QMainWindow { background: #f5f7fb; }
            QWidget { font-size: 14px; color: #172033; }
            QLabel#title { font-size: 26px; font-weight: 700; color: #123a70; }
            QLabel#subtitle { color: #5f6b7a; margin-bottom: 4px; }
            QLabel#hint { color: #667085; font-size: 12px; }
            QLabel#stage { color: #0b63ce; font-weight: 700; }
            QGroupBox {
                background: white;
                border: 1px solid #dce3ee;
                border-radius: 10px;
                margin-top: 12px;
                padding: 12px;
                font-weight: 700;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
            QLineEdit, QComboBox, QPlainTextEdit {
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 7px;
                background: white;
            }
            QPushButton {
                border: 1px solid #b9c6d8;
                border-radius: 7px;
                padding: 8px 14px;
                background: #ffffff;
            }
            QPushButton:hover { background: #eef5ff; }
            QPushButton:disabled { color: #98a2b3; background: #f2f4f7; }
            QPushButton#primary {
                color: white;
                background: #0b63ce;
                border-color: #0b63ce;
                font-weight: 700;
            }
            QPushButton#primary:hover { background: #0957b7; }
            QProgressBar {
                border: 1px solid #d0d8e5;
                border-radius: 6px;
                text-align: center;
                background: #eef2f7;
            }
            QProgressBar::chunk { background: #0b63ce; border-radius: 5px; }
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
