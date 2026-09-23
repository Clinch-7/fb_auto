from __future__ import annotations

import json
import sys
import threading
import traceback
from pathlib import Path

from runtime_env import configure_runtime_environment


configure_runtime_environment()

from PyQt5.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QIcon
from PyQt5.QtWidgets import (
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
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from playwright.sync_api import sync_playwright

from automation_lock import AUTOMATION_LOCK
from app_config import ensure_app_config_exists, load_app_config
from config import project_dir, runtime_dir
from flow import FlowContext, FlowRunError, HybridFlow
from main import EXPIRED_MESSAGE, enforce_expiration, is_expired
from roxy_client import (
    DEFAULT_ROXY_API_HOST,
    RoxyClient,
    RoxySettings,
    extract_cdp_endpoint,
    load_roxy_settings,
    save_roxy_settings,
)
from lark_command_listener import consume_events
from task_queue import claim_next_task, load_tasks, save_tasks, task_queue_path, update_task


def app_icon_path() -> str:
    candidates = [
        project_dir() / "app_icon.ico",
        project_dir().parent / "app_icon.ico",
    ]
    bundle_dir = getattr(sys, "_MEIPASS", "")
    if bundle_dir:
        candidates.append(Path(bundle_dir) / "app_icon.ico")
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return ""


def detect_facebook_cookie_accounts(client: RoxyClient, profile_ids: list[str] | None = None, open_profiles: bool = False) -> dict[str, str]:
    accounts: dict[str, str] = {}
    connections = []
    if open_profiles and profile_ids:
        for profile_id in profile_ids:
            try:
                connection = client.open_profile(profile_id)
                connection["dirId"] = connection.get("dirId") or profile_id
                connections.append(connection)
            except Exception:
                continue
    else:
        connections = client.connection_info(profile_ids)

    connection_by_profile = {
        str(row.get("dirId") or row.get("id") or ""): row
        for row in connections
        if isinstance(row, dict) and (row.get("dirId") or row.get("id"))
    }

    if not connection_by_profile:
        return accounts

    with sync_playwright() as playwright:
        for profile_id, connection in connection_by_profile.items():
            endpoint = extract_cdp_endpoint(connection)
            if not endpoint:
                continue
            browser = None
            try:
                browser = playwright.chromium.connect_over_cdp(endpoint)
                context = browser.contexts[0] if browser.contexts else None
                if not context:
                    continue
                page = context.pages[-1] if context.pages else context.new_page()
                try:
                    page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                cookies = context.cookies(
                    [
                        "https://www.facebook.com",
                        "https://developers.facebook.com",
                        "https://business.facebook.com",
                    ]
                )
                c_user = next((str(c.get("value")) for c in cookies if c.get("name") == "c_user" and c.get("value")), "")
                xs_exists = any(c.get("name") == "xs" and c.get("value") for c in cookies)
                if c_user:
                    suffix = "" if xs_exists else "（缺 xs）"
                    accounts[profile_id] = f"FB {c_user}{suffix}"
            except Exception:
                continue
            finally:
                if browser:
                    disconnect = getattr(browser, "disconnect", None)
                    if callable(disconnect):
                        disconnect()
                    else:
                        browser.close()
    return accounts


def discover_roxy_accounts(check_facebook: bool = False) -> tuple[list[dict], int]:
    client = RoxyClient(load_roxy_settings())
    profiles = client.list_profiles()
    facebook_accounts = (
        detect_facebook_cookie_accounts(
            client,
            [profile.dir_id for profile in profiles],
            open_profiles=True,
        )
        if check_facebook
        else {}
    )
    accounts = []
    for profile in profiles:
        facebook_label = facebook_accounts.get(profile.dir_id)
        label = f"{facebook_label} - {profile.display_name}" if facebook_label else profile.display_name
        accounts.append({"id": profile.dir_id, "label": label})
    return accounts, len(facebook_accounts)


def discover_local_accounts() -> list[dict]:
    accounts: set[str] = set()

    cookie_dirs = [runtime_dir(), project_dir().parent]
    for cookie_dir in cookie_dirs:
        for path in cookie_dir.glob("fb_cookies*.json"):
            try:
                with path.open("r", encoding="utf-8") as f:
                    cookies = json.load(f)
                c_user = next((str(c.get("value")) for c in cookies if c.get("name") == "c_user" and c.get("value")), "")
                if c_user:
                    accounts.add(c_user)
            except Exception:
                continue

    profile_root = runtime_dir() / "browser_profiles"
    if profile_root.exists():
        for profile in profile_root.iterdir():
            name = profile.name
            for prefix in ("chrome_", "edge_"):
                if name.startswith(prefix):
                    value = name[len(prefix) :]
                    if value.isdigit():
                        accounts.add(value)

    return [{"id": value, "label": value} for value in sorted(accounts)]


class AccountsRefreshWorker(QObject):
    done = pyqtSignal(object, int, bool)
    failed = pyqtSignal(str, object)

    def __init__(self, check_facebook: bool) -> None:
        super().__init__()
        self.check_facebook = check_facebook

    def run(self) -> None:
        try:
            accounts, facebook_count = discover_roxy_accounts(check_facebook=self.check_facebook)
            self.done.emit(accounts, facebook_count, self.check_facebook)
        except Exception as exc:
            local_accounts = discover_local_accounts()
            self.failed.emit(str(exc), local_accounts)


class FlowWorker(QObject):
    log = pyqtSignal(str)
    need_confirm = pyqtSignal(str)
    done = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, config: dict, status_only: bool, task_mode: bool = False) -> None:
        super().__init__()
        self.config = config
        self.status_only = status_only
        self.task_mode = task_mode
        self.confirm_event = threading.Event()

    def run(self) -> None:
        try:
            enforce_expiration()
            flow = HybridFlow(
                FlowContext(browser=self.config["browser"], account=self.config["account"]),
                logger=self.log.emit,
                manual_confirm=self.wait_for_confirm,
            )

            if self.status_only:
                flow.status()
                self.done.emit("登录状态检查完成")
                return

            if self.task_mode:
                with AUTOMATION_LOCK:
                    task = claim_next_task(roxy_profile_id=self.config["account"])
                    if not task:
                        self.done.emit("没有待处理的下架重建任务")
                        return
                    self.log.emit(f"处理任务：brand={task.brand}, old_app_id={task.old_app_id}, task_id={task.task_id}")
                    try:
                        fb_app_id = flow.run_brand(
                            brand_name=task.brand,
                            contact_email=None,
                            activity_class_name=None,
                            publish=self.config["publish"],
                            monitor_secret=None,
                            sync_monitor=self.config["monitor"],
                            adjust_brand_name=task.adjust_brand_name or task.brand,
                            monitor_email=task.roxy_profile_email,
                        )
                        update_task(task.task_id, status="success", new_app_id=fb_app_id, last_error="")
                        self.done.emit(f"任务完成，FB App ID: {fb_app_id}")
                        return
                    except Exception as exc:
                        detail = traceback.format_exc()
                        next_status = "failed" if task.attempts >= task.max_attempts else "pending"
                        created_app_id = exc.created_app_id if isinstance(exc, FlowRunError) else ""
                        update_task(task.task_id, status=next_status, new_app_id=created_app_id, last_error=detail)
                        raise

            with AUTOMATION_LOCK:
                fb_app_id = flow.run_brand(
                    brand_name=self.config["brand"],
                    contact_email=None,
                    activity_class_name=None,
                    publish=self.config["publish"],
                    monitor_secret=None,
                    sync_monitor=self.config["monitor"],
                    adjust_brand_name=self.config.get("adjust_brand_name") or self.config["brand"],
                )
            self.done.emit(f"执行完成，FB App ID: {fb_app_id}")
        except Exception:
            self.failed.emit(traceback.format_exc())

    def wait_for_confirm(self) -> None:
        self.confirm_event.clear()
        self.need_confirm.emit("等待人工处理：请完成浏览器里的所有 Continue、登录、密码或风控验证，然后点击“确认继续”。")
        self.confirm_event.wait()
        self.log.emit("已确认继续。")

    def confirm(self) -> None:
        self.confirm_event.set()


class LarkListenerWorker(QObject):
    log = pyqtSignal(str)
    done = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, worker_name: str, roxy_profile_id: str) -> None:
        super().__init__()
        self.worker_name = worker_name
        self.roxy_profile_id = roxy_profile_id
        self.stop_event = threading.Event()

    def run(self) -> None:
        try:
            self.log.emit(f"Lark 监听启动：worker={self.worker_name}, profile={self.roxy_profile_id}")
            while not self.stop_event.is_set():
                consume_events(
                    seconds=3600,
                    roxy_profile_id=self.roxy_profile_id,
                    worker_name=self.worker_name,
                    logger=self.log.emit,
                    stop_event=self.stop_event,
                )
            self.done.emit("Lark 监听已停止")
        except Exception:
            self.failed.emit(traceback.format_exc())

    def stop(self) -> None:
        self.stop_event.set()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.worker: FlowWorker | None = None
        self.thread: QThread | None = None
        self.listener_worker: LarkListenerWorker | None = None
        self.listener_thread: QThread | None = None
        self.refresh_worker: AccountsRefreshWorker | None = None
        self.refresh_thread: QThread | None = None
        self.refresh_previous_account: str | None = None

        self.setWindowTitle("Facebook 自动化工具")
        icon_path = app_icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(icon_path))
        self.resize(1060, 680)
        ensure_app_config_exists()
        self._build_ui()
        self._load_accounts()
        self._install_expiration_timer()

    def _install_expiration_timer(self) -> None:
        self.expiration_timer = QTimer(self)
        self.expiration_timer.setInterval(60_000)
        self.expiration_timer.timeout.connect(self._check_runtime_expiration)
        self.expiration_timer.start()

    def _check_runtime_expiration(self) -> None:
        if not is_expired():
            return
        self.expiration_timer.stop()
        if self.listener_worker:
            self.listener_worker.stop()
        QMessageBox.critical(self, "错误", EXPIRED_MESSAGE)
        app = QApplication.instance()
        if app:
            app.quit()

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)

        main = QHBoxLayout(root)
        main.setContentsMargins(18, 18, 18, 18)
        main.setSpacing(16)

        left = QFrame()
        left.setObjectName("panel")
        left.setFixedWidth(360)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(20, 20, 20, 20)
        left_layout.setSpacing(14)

        title = QLabel("Facebook 自动化工具")
        title.setObjectName("title")
        left_layout.addWidget(title)
        config_status = QLabel(self._config_status_text())
        config_status.setObjectName("hint")
        config_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        left_layout.addWidget(config_status)
        left_layout.addSpacing(8)

        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(12)

        self.brand_input = QLineEdit()
        self.brand_input.setPlaceholderText("输入完整品牌名，例如 qstand267")
        settings = load_roxy_settings()
        self.roxy_host_input = QLineEdit()
        self.roxy_host_input.setPlaceholderText(DEFAULT_ROXY_API_HOST)
        self.roxy_host_input.setText(settings.api_host or DEFAULT_ROXY_API_HOST)
        self.roxy_key_input = QLineEdit()
        self.roxy_key_input.setPlaceholderText("Roxy API Key，可空")
        self.roxy_key_input.setText(settings.api_key)
        self.account_combo = QComboBox()
        self.account_combo.setEditable(False)
        self.browser_combo = QComboBox()
        self.browser_combo.addItems(["roxy"])

        self.brand_label = self._add_row(form, 0, "品牌名", self.brand_input)
        self.roxy_host_label = self._add_row(form, 1, "Roxy地址", self.roxy_host_input)
        self.roxy_key_label = self._add_row(form, 2, "API Key", self.roxy_key_input)
        self.account_label = self._add_row(form, 3, "账号", self.account_combo)
        self.browser_label = self._add_row(form, 4, "浏览器", self.browser_combo)
        left_layout.addLayout(form)

        self.publish_check = QCheckBox("发布 App")
        self.publish_check.setChecked(True)
        self.monitor_check = QCheckBox("更新飞书监控表")
        self.monitor_check.setChecked(True)
        left_layout.addWidget(self.publish_check)
        left_layout.addWidget(self.monitor_check)

        button_row = QVBoxLayout()
        self.start_button = QPushButton("开始执行")
        self.start_button.setObjectName("primaryButton")
        self.status_button = QPushButton("检查登录")
        self.task_button = QPushButton("处理下架任务")
        self.clear_tasks_button = QPushButton("清空队列")
        self.listener_button = QPushButton("启动 Lark 监听")
        self.stop_listener_button = QPushButton("停止 Lark 监听")
        self.stop_listener_button.setEnabled(False)
        self.refresh_button = QPushButton("刷新账号")
        self.confirm_button = QPushButton("确认继续")
        self.confirm_button.setObjectName("confirmButton")
        self.confirm_button.setEnabled(False)
        button_row.addWidget(self.listener_button)
        button_row.addWidget(self.stop_listener_button)
        button_row.addWidget(self.refresh_button)
        left_layout.addLayout(button_row)

        self.brand_label.setVisible(False)
        self.brand_input.setVisible(False)
        self.publish_check.setVisible(False)
        self.monitor_check.setVisible(False)
        self.start_button.setVisible(False)
        self.status_button.setVisible(False)
        self.task_button.setVisible(False)
        self.clear_tasks_button.setVisible(False)
        self.confirm_button.setVisible(False)

        left_layout.addStretch()

        right = QFrame()
        right.setObjectName("panel")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(20, 20, 20, 20)
        right_layout.setSpacing(12)

        top = QHBoxLayout()
        log_title = QLabel("执行日志")
        log_title.setObjectName("sectionTitle")
        self.state_label = QLabel("空闲")
        self.state_label.setObjectName("stateIdle")
        top.addWidget(log_title)
        top.addStretch()
        top.addWidget(self.state_label)
        right_layout.addLayout(top)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 10))
        self.log_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        right_layout.addWidget(self.log_text)

        main.addWidget(left)
        main.addWidget(right, 1)

        self.start_button.clicked.connect(self.start_flow)
        self.status_button.clicked.connect(self.check_status)
        self.task_button.clicked.connect(self.run_repair_task)
        self.clear_tasks_button.clicked.connect(self.clear_task_queue)
        self.listener_button.clicked.connect(self.start_lark_listener)
        self.stop_listener_button.clicked.connect(self.stop_lark_listener)
        self.refresh_button.clicked.connect(self.refresh_accounts)
        self.confirm_button.clicked.connect(self.confirm_continue)

        self.setStyleSheet(STYLE)

    @staticmethod
    def _add_row(layout: QGridLayout, row: int, label: str, widget: QWidget) -> QLabel:
        qlabel = QLabel(label)
        qlabel.setObjectName("fieldLabel")
        layout.addWidget(qlabel, row, 0)
        layout.addWidget(widget, row, 1)
        return qlabel

    @staticmethod
    def _config_status_text() -> str:
        data = load_app_config()
        lark = data.get("lark", {}) if isinstance(data.get("lark"), dict) else {}
        sheet = data.get("monitor_sheet", {}) if isinstance(data.get("monitor_sheet"), dict) else {}
        lark_ready = bool(lark.get("openapi_base") and lark.get("app_id") and lark.get("app_secret"))
        sheet_ready = bool(sheet.get("spreadsheet_token") and sheet.get("sheet_id"))
        return (
            f"Lark机器人：{'已配置' if lark_ready else '未配置'}\n"
            f"Lark表格：{'已配置' if sheet_ready else '未配置'}"
        )

    def _load_accounts(self, check_facebook: bool = False) -> None:
        self._save_roxy_settings_from_form()
        current = self.account_combo.currentData()
        try:
            accounts, facebook_count = discover_roxy_accounts(check_facebook=check_facebook)
            self._apply_roxy_accounts(accounts, current)
            if check_facebook:
                self._append_log(f"已读取 Roxy Profile：{len(accounts)} 个；Facebook 检测到登录：{facebook_count} 个")
            else:
                self._append_log(f"已读取 Roxy Profile：{len(accounts)} 个")
        except Exception as exc:
            self._append_log(f"读取 Roxy 账号失败：{exc}")
            accounts = discover_local_accounts()
            if accounts:
                self._apply_local_accounts(accounts, current)
                self._append_log(f"已读取本地备用账号：{len(accounts)} 个")
            else:
                self.account_combo.clear()
                self.browser_combo.clear()
                self.browser_combo.addItems(["roxy"])

    def _apply_roxy_accounts(self, accounts: list[dict], current: str | None = None) -> None:
        self.account_combo.clear()
        self.browser_combo.clear()
        self.browser_combo.addItems(["roxy"])
        for account in accounts:
            self.account_combo.addItem(account["label"], account["id"])
        self._restore_selected_account(current, accounts)

    def _apply_local_accounts(self, accounts: list[dict], current: str | None = None) -> None:
        self.account_combo.clear()
        for account in accounts:
            self.account_combo.addItem(f"{account['label']}（本地备用）", account["id"])
        self.browser_combo.clear()
        self.browser_combo.addItems(["chrome"])
        self._restore_selected_account(current, accounts)

    def _restore_selected_account(self, current: str | None, accounts: list[dict]) -> None:
        if current:
            index = self.account_combo.findData(current)
            if index >= 0:
                self.account_combo.setCurrentIndex(index)
                return
        if accounts:
            self.account_combo.setCurrentIndex(0)

    def refresh_accounts(self) -> None:
        if self.refresh_thread and self.refresh_thread.isRunning():
            self._append_log("刷新账号仍在进行中，请稍等。")
            return
        self._append_log("刷新账号：正在后台打开 Roxy Profile 并访问 Facebook 检测登录...")
        self._save_roxy_settings_from_form()
        self.refresh_previous_account = self.account_combo.currentData()
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("刷新中...")

        self.refresh_thread = QThread()
        self.refresh_worker = AccountsRefreshWorker(check_facebook=True)
        self.refresh_worker.moveToThread(self.refresh_thread)
        self.refresh_thread.started.connect(self.refresh_worker.run)
        self.refresh_worker.done.connect(self._refresh_accounts_done)
        self.refresh_worker.failed.connect(self._refresh_accounts_failed)
        self.refresh_worker.done.connect(self.refresh_thread.quit)
        self.refresh_worker.failed.connect(self.refresh_thread.quit)
        self.refresh_thread.finished.connect(self.refresh_worker.deleteLater)
        self.refresh_thread.finished.connect(self.refresh_thread.deleteLater)
        self.refresh_thread.finished.connect(self._refresh_thread_finished)
        self.refresh_thread.start()

    def _refresh_accounts_done(self, accounts: list[dict], facebook_count: int, check_facebook: bool) -> None:
        current = self.refresh_previous_account
        self._apply_roxy_accounts(accounts, current)
        if check_facebook:
            self._append_log(f"已读取 Roxy Profile：{len(accounts)} 个；Facebook 检测到登录：{facebook_count} 个")
        else:
            self._append_log(f"已读取 Roxy Profile：{len(accounts)} 个")

    def _refresh_accounts_failed(self, message: str, accounts: list[dict]) -> None:
        current = self.refresh_previous_account
        self._append_log(f"读取 Roxy 账号失败：{message}")
        if accounts:
            self._apply_local_accounts(accounts, current)
            self._append_log(f"已读取本地备用账号：{len(accounts)} 个")
        else:
            self.account_combo.clear()
            self.browser_combo.clear()
            self.browser_combo.addItems(["roxy"])

    def _refresh_thread_finished(self) -> None:
        self.refresh_worker = None
        self.refresh_thread = None
        self.refresh_previous_account = None
        running = bool(self.thread and self.thread.isRunning())
        self.refresh_button.setEnabled(not running)
        self.refresh_button.setText("刷新账号")

    def _save_roxy_settings_from_form(self) -> None:
        host = self.roxy_host_input.text().strip() or DEFAULT_ROXY_API_HOST
        key = self.roxy_key_input.text().strip()
        save_roxy_settings(RoxySettings(api_host=host, api_key=key))

    def check_status(self) -> None:
        self._start_worker(status_only=True)

    def run_repair_task(self) -> None:
        self._start_worker(status_only=False, task_mode=True)

    def clear_task_queue(self) -> None:
        count = len(load_tasks())
        save_tasks([])
        self._append_log(f"已清空任务队列：{count} 个，路径：{task_queue_path()}")

    def start_flow(self) -> None:
        if not self.brand_input.text().strip():
            QMessageBox.warning(self, "缺少品牌名", "请先填写完整品牌名。")
            return
        self._start_worker(status_only=False)

    def start_lark_listener(self) -> None:
        self._save_roxy_settings_from_form()
        if self.listener_thread and self.listener_thread.isRunning():
            QMessageBox.information(self, "正在监听", "Lark 监听已经启动。")
            return
        worker_name = ""
        account = self.account_combo.currentData() or self.account_combo.currentText().strip()
        if not account:
            QMessageBox.warning(self, "没有账号", "请先刷新并选择 Roxy Profile。")
            return

        self.listener_thread = QThread()
        self.listener_worker = LarkListenerWorker(worker_name=worker_name, roxy_profile_id=account)
        self.listener_worker.moveToThread(self.listener_thread)
        self.listener_thread.started.connect(self.listener_worker.run)
        self.listener_worker.log.connect(self._append_log)
        self.listener_worker.done.connect(self._listener_done)
        self.listener_worker.failed.connect(self._listener_failed)
        self.listener_worker.done.connect(self.listener_thread.quit)
        self.listener_worker.failed.connect(self.listener_thread.quit)
        self.listener_thread.finished.connect(self.listener_worker.deleteLater)
        self.listener_thread.finished.connect(self.listener_thread.deleteLater)
        self.listener_thread.finished.connect(self._listener_thread_finished)
        self.listener_thread.start()
        self.listener_button.setEnabled(False)
        self.stop_listener_button.setEnabled(True)
        self.account_combo.setEnabled(False)
        self._append_log("命令格式：配置qstand237")
        self._append_log("查看可用邮箱：查看邮箱")

    def stop_lark_listener(self) -> None:
        if self.listener_worker:
            self.listener_worker.stop()
        self.stop_listener_button.setEnabled(False)
        self._append_log("正在停止 Lark 监听...")

    def _start_worker(self, status_only: bool, task_mode: bool = False) -> None:
        self._save_roxy_settings_from_form()
        account = self.account_combo.currentData() or self.account_combo.currentText().strip()
        if not account:
            QMessageBox.warning(self, "没有账号", "没有读取到 Roxy profile。请先打开 Roxy，并检查 API 地址/API Key。")
            return

        if self.thread and self.thread.isRunning():
            QMessageBox.information(self, "正在执行", "当前任务还在执行中。")
            return

        config = {
            "brand": self.brand_input.text().strip(),
            "account": account,
            "browser": self.browser_combo.currentText().strip(),
            "publish": self.publish_check.isChecked(),
            "monitor": self.monitor_check.isChecked(),
        }

        self._set_running(True)
        if status_only:
            start_message = "检查登录状态..."
        elif task_mode:
            start_message = "开始处理下架任务..."
        else:
            start_message = "开始执行..."
        self._append_log(start_message)

        self.thread = QThread()
        self.worker = FlowWorker(config, status_only=status_only, task_mode=task_mode)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self._append_log)
        self.worker.need_confirm.connect(self._need_confirm)
        self.worker.done.connect(self._done)
        self.worker.failed.connect(self._failed)
        self.worker.done.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(self._load_accounts)
        self.thread.start()

    def _set_running(self, running: bool) -> None:
        refreshing = bool(self.refresh_thread and self.refresh_thread.isRunning())
        self.start_button.setEnabled(not running)
        self.status_button.setEnabled(not running)
        self.task_button.setEnabled(not running)
        self.clear_tasks_button.setEnabled(not running)
        self.refresh_button.setEnabled(not running and not refreshing)
        self.confirm_button.setEnabled(False)
        self._set_state("执行中" if running else "空闲", "stateRunning" if running else "stateIdle")

    def _set_state(self, text: str, object_name: str) -> None:
        self.state_label.setText(text)
        self.state_label.setObjectName(object_name)
        self.state_label.style().unpolish(self.state_label)
        self.state_label.style().polish(self.state_label)

    def _need_confirm(self, message: str) -> None:
        self._append_log(message)
        self.confirm_button.setEnabled(True)
        self._set_state("等待确认", "stateWait")

    def confirm_continue(self) -> None:
        if self.worker:
            self.worker.confirm()
        self.confirm_button.setEnabled(False)
        self._set_state("执行中", "stateRunning")

    def _done(self, message: str) -> None:
        self._append_log(message)
        self._set_running(False)

    def _failed(self, detail: str) -> None:
        self._append_log("[错误]\n" + detail)
        self._set_running(False)

    def _listener_done(self, message: str) -> None:
        self._append_log(message)

    def _listener_failed(self, detail: str) -> None:
        self._append_log("[Lark监听错误]\n" + detail)

    def _listener_thread_finished(self) -> None:
        self.listener_button.setEnabled(True)
        self.stop_listener_button.setEnabled(False)
        self.account_combo.setEnabled(True)

    def _append_log(self, message: str) -> None:
        self.log_text.append(message)
        self.log_text.moveCursor(self.log_text.textCursor().End)


STYLE = """
QMainWindow { background: #eef2f7; }
QFrame#panel {
    background: #ffffff;
    border: 1px solid #d9e1ec;
    border-radius: 10px;
}
QLabel#title {
    color: #1267d8;
    font-size: 22px;
    font-weight: 700;
}
QLabel#subtitle, QLabel#hint {
    color: #65758b;
    font-size: 12px;
}
QLabel#sectionTitle {
    color: #172033;
    font-size: 18px;
    font-weight: 700;
}
QLabel#fieldLabel {
    color: #253044;
    font-weight: 600;
}
QLineEdit, QComboBox {
    min-height: 34px;
    border: 1px solid #cfd8e6;
    border-radius: 7px;
    padding: 4px 9px;
    background: #fbfdff;
    color: #172033;
}
QLineEdit:focus, QComboBox:focus {
    border: 1px solid #1b74e4;
    background: #ffffff;
}
QCheckBox {
    color: #253044;
    spacing: 8px;
}
QPushButton {
    min-height: 38px;
    border-radius: 8px;
    border: 1px solid #cbd5e1;
    background: #f8fafc;
    color: #1f2937;
    font-weight: 650;
}
QPushButton:hover {
    background: #eef4ff;
    border-color: #9ec5ff;
}
QPushButton:disabled {
    color: #9aa6b5;
    background: #edf1f6;
}
QPushButton#primaryButton {
    background: #1b74e4;
    color: #ffffff;
    border: 1px solid #1b74e4;
}
QPushButton#primaryButton:hover { background: #1667cc; }
QPushButton#confirmButton {
    background: #12a150;
    color: #ffffff;
    border: 1px solid #12a150;
}
QPushButton#confirmButton:disabled {
    background: #e8eef5;
    color: #9aa6b5;
    border: 1px solid #d3dce8;
}
QTextEdit {
    border: 1px solid #d9e1ec;
    border-radius: 8px;
    background: #0f172a;
    color: #dbeafe;
    padding: 10px;
}
QLabel#stateIdle, QLabel#stateRunning, QLabel#stateWait {
    border-radius: 999px;
    padding: 5px 12px;
    font-weight: 700;
}
QLabel#stateIdle {
    color: #475569;
    background: #e2e8f0;
}
QLabel#stateRunning {
    color: #075985;
    background: #d8efff;
}
QLabel#stateWait {
    color: #8a4b00;
    background: #ffedbf;
}
"""


def main() -> None:
    if "--automation-runner" in sys.argv:
        args = [arg for arg in sys.argv[1:] if arg != "--automation-runner"]
        sys.argv = [sys.argv[0], *args]
        from automation_runner import main as automation_runner_main

        automation_runner_main()
        return

    if "--lark-sdk-event-source" in sys.argv:
        sys.argv = [sys.argv[0], *[arg for arg in sys.argv[1:] if arg != "--lark-sdk-event-source"]]
        from lark_sdk_event_source import main as sdk_event_source_main

        sdk_event_source_main()
        return

    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    icon_path = app_icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(icon_path))
    try:
        enforce_expiration()
    except SystemExit:
        QMessageBox.critical(None, "错误", EXPIRED_MESSAGE)
        sys.exit(1)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
