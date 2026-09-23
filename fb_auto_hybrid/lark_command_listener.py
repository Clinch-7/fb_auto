from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import requests

from app_config import value
from automation_lock import AUTOMATION_LOCK
from config import project_dir, runtime_dir
from main import EXPIRED_MESSAGE, assert_not_expired
from monitor_sheet import MonitorSheet, monitor_name_for_brand
from roxy_client import RoxyClient, load_roxy_settings
from runtime_env import configure_runtime_environment
from task_queue import claim_next_task, claim_task, enqueue_brand_task, update_task
from vest_service import VestService


configure_runtime_environment()

LARK_CLI_CANDIDATES = [
    r"C:\nvm4w\nodejs\lark-cli.cmd",
    r"C:\Program Files\nodejs\lark-cli.cmd",
]
LARK_REQUEST_TIMEOUT = (3, 8)
LARK_LISTENER_LOCK_PORT = 49271
PROCESSED_EVENT_IDS_LIMIT = 2000
HISTORICAL_EVENT_GRACE_SECONDS = 5
RESULT_MARKER = "__FB_AUTO_RESULT__"
_SERIAL_WORK_QUEUE: queue.Queue[dict[str, Any]] = queue.Queue()
_SERIAL_WORKER_LOCK = threading.Lock()
_SERIAL_WORKER_STARTED = False
_lark_token_cache = {"token": "", "expire_at": 0.0}
COMMAND_PATTERN = re.compile(
    r"(?:^|\s|@[\w\-\u4e00-\u9fff]+)\s*配置\s*([^\s]+)(?:\s+([^\s]+))?(?:\s+([^\s]+))?",
    re.I,
)
CONFIG_OVERRIDE_PATTERN = re.compile(
    r"(?:^|\s|@[\w\-\u4e00-\u9fff]+)\s*配置\s+([^\s]+)\s+覆盖\s+([^\s]+)(?:\s+([^\s]+))?",
    re.I,
)
PROFILE_LIST_PATTERN = re.compile(
    r"^\s*(?:@.+?\s+)?(?:选择|查看|列出)?\s*(?:profile|邮箱)\s*$",
    re.I,
)
AD_AUTH_PATTERN = re.compile(
    r"(?:^|\s|@[\w\-\u4e00-\u9fff]+)\s*(?:授权广告|广告授权|授权)\s+(.+)$",
    re.I | re.S,
)
AD_REMOVE_PATTERN = re.compile(
    r"(?:^|\s|@[\w\-\u4e00-\u9fff]+)\s*删除\s+(.+)$",
    re.I | re.S,
)
AD_REMOVE_ALL_PATTERN = re.compile(
    r"(?:^|\s|@[\w\-\u4e00-\u9fff]+)\s*清空\s+(.+)$",
    re.I | re.S,
)
TT_PATTERN = re.compile(
    r"(?:^|\s|@[\w\-\u4e00-\u9fff]+)\s*tt\s+(.+)$",
    re.I | re.S,
)
LIST_SEPARATOR_RE = r"[,，、]+"
ITEM_SEPARATOR_RE = r"[\s,，、]+"


def listener_state_path() -> Path:
    return runtime_dir() / "lark_listener_state.json"


@contextmanager
def lark_listener_lock():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", LARK_LISTENER_LOCK_PORT))
        sock.listen(1)
    except OSError as exc:
        sock.close()
        raise RuntimeError("已有 Lark 监听在运行，请先停止旧监听或关闭旧工具窗口。") from exc
    try:
        yield
    finally:
        sock.close()


def load_state() -> dict[str, Any]:
    path = listener_state_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict[str, Any]) -> None:
    path = listener_state_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def remember_processed_event(state: dict[str, Any], event_id: str) -> None:
    if not event_id:
        return
    processed = list(state.get("processed_event_ids", []))
    if event_id not in processed:
        processed.append(event_id)
    state["processed_event_ids"] = processed[-PROCESSED_EVENT_IDS_LIMIT:]


def parse_event_create_time_ms(event: dict[str, Any]) -> int | None:
    raw = (
        event.get("create_time")
        or event.get("message_create_time")
        or event.get("timestamp")
        or event.get("time")
    )
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    try:
        value = int(digits)
    except ValueError:
        return None
    if value < 10_000_000_000:
        return value * 1000
    return value


def should_ignore_historical_event(event: dict[str, Any], state: dict[str, Any]) -> bool:
    started_at_ms = int(state.get("listener_started_at_ms", 0) or 0)
    created_at_ms = parse_event_create_time_ms(event)
    if not started_at_ms or not created_at_ms:
        return False
    return created_at_ms < started_at_ms - HISTORICAL_EVENT_GRACE_SECONDS * 1000


def brand_name_from_text(value: str) -> str:
    return str(value or "").strip()


def looks_like_email(value: str) -> bool:
    text = normalize_email(value)
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text))


def extract_email(value: str) -> str:
    match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", str(value or ""))
    return normalize_email(match.group(0)) if match else ""


def normalize_email(value: str) -> str:
    text = str(value or "").strip().strip("<>，,。、；;:：")
    text = text.replace("\u200b", "").replace("\ufeff", "")
    return text.lower()


def profile_email(profile) -> str:
    values = [
        getattr(profile, "name", ""),
        getattr(profile, "user_name", ""),
        getattr(profile, "remark", ""),
        getattr(profile, "display_name", ""),
    ]
    for value in values:
        email = extract_email(str(value or ""))
        if email:
            return email
    return ""


def parse_command(content: str) -> tuple[str, str, str, str] | None:
    raw_text = str(content or "")
    match = COMMAND_PATTERN.search(raw_text)
    if not match:
        return None
    value = match.group(1).strip()
    worker_or_email = (match.group(2) or "").strip()
    explicit_email = (match.group(3) or "").strip()
    worker_name = ""
    roxy_profile_email = extract_email(raw_text)

    if roxy_profile_email:
        if worker_or_email and not looks_like_email(worker_or_email):
            worker_name = worker_or_email
    elif explicit_email:
        worker_name = "" if looks_like_email(worker_or_email) else worker_or_email
        roxy_profile_email = normalize_email(explicit_email) if looks_like_email(explicit_email) else ""
    elif worker_or_email:
        if looks_like_email(worker_or_email):
            roxy_profile_email = normalize_email(worker_or_email)
        else:
            worker_name = worker_or_email

    return brand_name_from_text(value), value, worker_name, roxy_profile_email


def parse_config_override_command(content: str) -> tuple[str, str, str] | None:
    raw_text = str(content or "")
    match = CONFIG_OVERRIDE_PATTERN.search(raw_text)
    if not match:
        return None
    brand = brand_name_from_text(match.group(1))
    override_brand = brand_name_from_text(match.group(2))
    roxy_profile_email = extract_email(raw_text)
    explicit = (match.group(3) or "").strip()
    if not roxy_profile_email and looks_like_email(explicit):
        roxy_profile_email = normalize_email(explicit)
    return (brand, override_brand, roxy_profile_email) if brand and override_brand else None


def is_profile_list_command(content: str) -> bool:
    text = str(content or "").strip()
    if PROFILE_LIST_PATTERN.search(text):
        return True
    compact = re.sub(r"\s+", "", text).lower()
    return any(keyword in compact for keyword in ("选择profile", "查看profile", "列出profile", "查看邮箱", "选择邮箱", "列出邮箱"))


def clean_command_content(content: str) -> str:
    text = str(content or "")
    text = re.sub(r"<at\b[^>]*>.*?</at>", " ", text, flags=re.I | re.S)
    text = re.sub(r"(?<![\w.+-])@[\w\-\u4e00-\u9fff]+", " ", text)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"[ \t]+", " ", text).strip()


def normalize_lark_text(text: str) -> str:
    safe_text = str(text or "").replace("|", "｜")
    lines = [line.strip() for line in safe_text.replace("\r", "\n").split("\n") if line.strip()]
    return " ｜ ".join(lines) if lines else ""


def _parse_ad_account_command(pattern: re.Pattern[str], content: str) -> tuple[list[str], list[str], str] | None:
    match = pattern.search(str(content or "").strip())
    if not match:
        return None
    body = match.group(1).strip()
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if len(lines) < 2:
        return None

    first_line = lines[0]
    ids_text = "\n".join(lines[1:])
    roxy_profile_email = extract_email(first_line)
    if roxy_profile_email:
        first_line = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", " ", first_line)

    app_or_brand_values = [
        value.strip()
        for value in re.split(ITEM_SEPARATOR_RE, first_line)
        if value.strip()
    ]

    ad_account_ids: list[str] = []
    seen: set[str] = set()
    for value in re.split(ITEM_SEPARATOR_RE, ids_text):
        if not re.fullmatch(r"\d{6,}", value) or value in seen:
            continue
        seen.add(value)
        ad_account_ids.append(value)

    return (app_or_brand_values, ad_account_ids, roxy_profile_email) if app_or_brand_values and ad_account_ids else None


def parse_ad_auth_command(content: str) -> tuple[list[str], list[str], str] | None:
    return _parse_ad_account_command(AD_AUTH_PATTERN, content)


def parse_ad_remove_command(content: str) -> tuple[list[str], list[str], str] | None:
    return _parse_ad_account_command(AD_REMOVE_PATTERN, content)


def parse_ad_remove_all_command(content: str) -> tuple[list[str], str] | None:
    match = AD_REMOVE_ALL_PATTERN.search(str(content or "").strip())
    if not match:
        return None
    body = match.group(1).strip()
    roxy_profile_email = extract_email(body)
    if roxy_profile_email:
        body = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", " ", body)
    app_or_brand_values = [
        value.strip()
        for value in re.split(ITEM_SEPARATOR_RE, body)
        if value.strip()
    ]
    return (app_or_brand_values, roxy_profile_email) if app_or_brand_values else None


def parse_tiktok_app_id_command(content: str) -> list[tuple[str, str]] | None:
    match = TT_PATTERN.search(str(content or "").strip())
    if not match:
        return None
    body = match.group(1).strip()
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if len(lines) < 2:
        return None

    result: list[tuple[str, str]] = []
    current_brands: list[str] = []
    current_ids: list[str] = []
    current_seen_ids: set[str] = set()

    def flush_current() -> None:
        nonlocal current_brands, current_ids, current_seen_ids
        if current_brands and current_ids:
            ids_text = ",".join(current_ids)
            for brand in current_brands:
                result.append((brand, ids_text))
        current_brands = []
        current_ids = []
        current_seen_ids = set()

    for line in lines:
        is_id_line = bool(re.fullmatch(r"[\d\s,，、]+", line)) or bool(
            current_brands and re.match(r"(?i)^id\s*[:：]", line)
        )
        if is_id_line:
            for value in re.findall(r"\d+", line):
                clean_value = value.strip()
                if not clean_value or clean_value in current_seen_ids:
                    continue
                current_seen_ids.add(clean_value)
                current_ids.append(clean_value)
            continue

        flush_current()
        current_brands = [
            value.strip()
            for value in re.split(ITEM_SEPARATOR_RE, line)
            if value.strip()
        ]

    flush_current()
    return result or None


def resolve_fb_app_id(value: str, logger: callable | None = None) -> tuple[str, str]:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{10,}", text):
        return text, "app_id"

    brand = text
    monitor_name = monitor_name_for_brand(brand)
    try:
        rows = MonitorSheet().read_rows()
        for row in rows:
            name = str(row[0]).strip() if len(row) >= 1 and row[0] else ""
            app_id = str(row[1]).strip() if len(row) >= 2 and row[1] else ""
            if name == monitor_name and app_id:
                return app_id, f"monitor:{monitor_name}"
    except Exception as exc:
        if logger:
            logger(f"[ad-auth] monitor lookup failed: {exc}")

    vest = VestService()
    vest.login()
    app = vest.get_brand_app(brand)
    app_id = str(app.raw.get("extension_info", {}).get("fb_app_code") or "").strip()
    if not app_id:
        raise RuntimeError(f"品牌 {brand} 没有找到 Facebook App ID")
    return app_id, f"vest:{brand}"


def resolve_monitor_app_and_email(value: str, logger: callable | None = None) -> tuple[str, str, str]:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{10,}", text):
        return text, "", "app_id"

    brand = text
    monitor_name = monitor_name_for_brand(brand)
    try:
        row = MonitorSheet().find_brand(monitor_name)
        if row:
            app_id = str(row.get("app_id") or "").strip()
            email = normalize_email(str(row.get("email") or ""))
            if app_id:
                return app_id, email, f"monitor:{monitor_name}"
    except Exception as exc:
        if logger:
            logger(f"[ad-auth] monitor lookup failed: {exc}")

    vest = VestService()
    vest.login()
    app = vest.get_brand_app(brand)
    app_id = str(app.raw.get("extension_info", {}).get("fb_app_code") or "").strip()
    if not app_id:
        raise RuntimeError(f"品牌 {brand} 没有找到 Facebook App ID")
    return app_id, "", f"vest:{brand}"


def send_lark_text(chat_id: str, text: str, logger: callable | None = None) -> bool:
    if not chat_id:
        return False
    text = normalize_lark_text(text)
    if not text:
        return False
    app_id, app_secret = _lark_config()
    if app_id and app_secret:
        return _send_lark_text_openapi(chat_id, text, logger=logger)

    if _send_lark_text_openapi(chat_id, text, logger=logger):
        return True
    try:
        result = subprocess.run(
            [lark_cli_path(), "im", "+messages-send", "--chat-id", chat_id, "--text", text, "--as", "bot"],
            cwd=str(Path(__file__).resolve().parent),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            if logger:
                logger(f"[lark] send failed code={result.returncode} stdout={result.stdout[:300]} stderr={result.stderr[:300]}")
            return False
        if logger:
            logger(f"[lark] sent: {text[:300]}")
        return True
    except Exception as exc:
        if logger:
            logger(f"[lark] send exception: {exc}")
        return False


def _lark_config() -> tuple[str, str]:
    app_id = str(value("lark", "app_id") or value("monitor_sheet", "app_id") or "").strip()
    app_secret = str(value("lark", "app_secret") or value("monitor_sheet", "app_secret") or "").strip()
    return app_id, app_secret


def prefer_sdk_listener() -> bool:
    configured = value("lark", "prefer_sdk_listener", None)
    if configured is not None:
        return str(configured).strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _lark_openapi_base() -> str:
    base = str(value("lark", "openapi_base", "https://open.larksuite.com") or "https://open.larksuite.com").strip()
    if not base.startswith(("http://", "https://")):
        base = "https://open.larksuite.com"
    return base.rstrip("/")


def _get_lark_tenant_token() -> str:
    app_id, app_secret = _lark_config()
    if not app_id or not app_secret:
        return ""

    now = time.time()
    if _lark_token_cache["token"] and now < _lark_token_cache["expire_at"] - 60:
        return str(_lark_token_cache["token"])

    resp = requests.post(
        f"{_lark_openapi_base()}/open-apis/auth/v3/tenant_access_token/internal",
        headers={"Content-Type": "application/json"},
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=LARK_REQUEST_TIMEOUT,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取 Lark Token 失败: {data}")
    token = str(data.get("tenant_access_token") or "")
    _lark_token_cache["token"] = token
    _lark_token_cache["expire_at"] = now + int(data.get("expire", 7200))
    return token


def _send_lark_text_openapi(chat_id: str, text: str, logger: callable | None = None) -> bool:
    try:
        token = _get_lark_tenant_token()
        if not token:
            return False
        resp = requests.post(
            f"{_lark_openapi_base()}/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
            timeout=LARK_REQUEST_TIMEOUT,
        )
        data = resp.json()
        if data.get("code") == 0:
            if logger:
                logger(f"[lark] sent: {text[:300]}")
            return True
        if logger:
            logger(f"[lark] OpenAPI send failed: {data}")
    except Exception as exc:
        if logger:
            logger(f"[lark] OpenAPI send exception: {exc}")
    return False


def compact_error_reason(detail: str, exc: Exception | None = None) -> str:
    current = exc
    seen: set[int] = set()
    while current and id(current) not in seen:
        seen.add(id(current))
        if current.__class__.__name__ != "FlowRunError":
            text = str(current).strip()
            if text:
                return text[:500]
        current = current.__cause__ or current.__context__

    if exc:
        text = str(exc).strip()
        if text:
            return text[:500]

    lines = [line.strip() for line in str(detail or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if ":" in line and (
            line.startswith("RuntimeError:")
            or line.startswith("ValueError:")
            or line.startswith("meta_api.")
            or line.startswith("flow.")
        ):
            return line.split(":", 1)[-1].strip()[:500] or line[:500]
    return (lines[-1] if lines else "未知错误")[:500]


def lark_cli_path() -> str:
    bundled_candidates = [
        project_dir() / "lark-cli" / "lark-cli.exe",
        project_dir() / "lark-cli" / "lark-cli.cmd",
        project_dir() / "cli" / "lark-cli.exe",
        project_dir() / "cli" / "lark-cli.cmd",
    ]
    for candidate in bundled_candidates:
        if candidate.exists():
            return str(candidate)

    found = shutil.which("lark-cli") or shutil.which("lark-cli.cmd")
    if found:
        return found
    for candidate in LARK_CLI_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise FileNotFoundError("未找到 lark-cli，请先在本机安装并配置 lark-cli。")


def _lark_cli_brand() -> str:
    base = _lark_openapi_base().lower()
    return "lark" if "larksuite" in base else "feishu"


def ensure_lark_cli_configured(cli_path: str, logger: callable | None = None) -> None:
    app_id, app_secret = _lark_config()
    if not app_id or not app_secret:
        return

    show = subprocess.run(
        [cli_path, "config", "show"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    if show.returncode == 0 and app_id in (show.stdout or ""):
        return

    if logger:
        logger("[lark] lark-cli not configured for this app; initializing from app_config.json")
    init = subprocess.run(
        [
            cli_path,
            "config",
            "init",
            "--app-id",
            app_id,
            "--app-secret-stdin",
            "--brand",
            _lark_cli_brand(),
        ],
        input=app_secret,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if init.returncode != 0:
        detail = (init.stderr or init.stdout or "").strip()[:500]
        raise RuntimeError(f"lark-cli 初始化失败: {detail}")


def resolve_roxy_profile_by_email(email: str) -> tuple[str, str]:
    target = normalize_email(email)
    if not target:
        return "", ""
    profiles = RoxyClient(load_roxy_settings()).list_profiles()
    matches = [profile for profile in profiles if profile_email(profile) == target]
    if not matches:
        raise RuntimeError(f"没有找到 Roxy 邮箱：{email}。请发送“查看邮箱”确认可用邮箱。")
    if len(matches) > 1:
        names = ", ".join(profile.display_name for profile in matches[:5])
        raise RuntimeError(f"Roxy 邮箱重复：{email}，匹配到 {len(matches)} 个 Profile：{names}")
    profile = matches[0]
    return profile.dir_id, profile_email(profile) or email


def resolve_roxy_profile_email_by_id(profile_id: str) -> str:
    target = str(profile_id or "").strip()
    if not target:
        return ""
    profiles = RoxyClient(load_roxy_settings()).list_profiles()
    for profile in profiles:
        if profile.dir_id == target:
            return profile_email(profile)
    return ""


def send_profile_list(chat_id: str, selected_profile_id: str = "", logger: callable | None = None) -> None:
    try:
        profiles = RoxyClient(load_roxy_settings()).list_profiles()
    except Exception as exc:
        send_lark_text(chat_id, f"读取 Roxy Profile 失败：{exc}", logger=logger)
        return

    if not profiles:
        send_lark_text(chat_id, "当前工具没有读取到 Roxy Profile。请确认 Roxy 已打开且 API 可用。", logger=logger)
        return

    lines = ["当前可用邮箱："]
    for index, profile in enumerate(profiles, start=1):
        marker = "（当前选择）" if profile.dir_id == selected_profile_id else ""
        email = profile_email(profile) or "未检测到邮箱"
        detail = profile.display_name
        lines.append(f"{index}. {email}{marker}")
        lines.append(f"   Profile：{detail}")
    lines.append("")
    lines.append("配置命令：配置 qstand237 邮箱")
    sent = send_lark_text(chat_id, "\n".join(lines), logger=logger)
    if logger:
        logger(f"[lark] email list sent={sent}; count={len(profiles)}")


class AutomationSubprocessError(RuntimeError):
    def __init__(self, message: str, *, detail: str = "", created_app_id: str = "", cleanup_message: str = "") -> None:
        super().__init__(message)
        self.detail = detail
        self.created_app_id = created_app_id
        self.cleanup_message = cleanup_message


def _automation_runner_command(payload_path: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--automation-runner", "--payload", str(payload_path)]
    return [sys.executable, str(Path(__file__).with_name("automation_runner.py")), "--payload", str(payload_path)]


def _run_automation_subprocess(payload: dict[str, Any], logger: callable | None = None) -> dict[str, Any]:
    runtime = runtime_dir()
    payload_path = runtime / f"automation_payload_{int(time.time() * 1000)}_{uuid.uuid4().hex}.json"
    with payload_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    command = _automation_runner_command(payload_path)
    stderr_lines: list[str] = []
    stdout_lines: list[str] = []
    result_payload: dict[str, Any] | None = None
    child_env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }

    process = subprocess.Popen(
        command,
        cwd=str(Path(__file__).resolve().parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
    )

    def pump_stderr() -> None:
        if not process.stderr:
            return
        for line in iter(process.stderr.readline, ""):
            text = line.rstrip()
            if not text:
                continue
            stderr_lines.append(text)
            if logger:
                logger(text)

    stderr_thread = threading.Thread(target=pump_stderr, daemon=True)
    stderr_thread.start()

    if process.stdout:
        for line in iter(process.stdout.readline, ""):
            text = line.rstrip()
            if not text:
                continue
            stdout_lines.append(text)
            if text.startswith(RESULT_MARKER):
                try:
                    result_payload = json.loads(text[len(RESULT_MARKER) :])
                except Exception:
                    pass
            elif logger:
                logger(text)

    return_code = process.wait()
    stderr_thread.join(timeout=2)
    try:
        payload_path.unlink()
    except Exception:
        pass

    if result_payload is None:
        detail = "\n".join(stdout_lines + stderr_lines)
        raise AutomationSubprocessError(
            f"automation subprocess failed without result, exit={return_code}",
            detail=detail,
        )
    if not result_payload.get("success"):
        raise AutomationSubprocessError(
            str(result_payload.get("error") or "automation subprocess failed"),
            detail=str(result_payload.get("detail") or ""),
            created_app_id=str(result_payload.get("created_app_id") or ""),
            cleanup_message=str(result_payload.get("cleanup_message") or ""),
        )
    if return_code != 0:
        raise AutomationSubprocessError(
            f"automation subprocess exited with code {return_code}",
            detail="\n".join(stdout_lines + stderr_lines),
        )
    return result_payload


def run_brand_task(
    task_id: str,
    brand: str,
    adjust_brand_name: str,
    override_brand: str,
    roxy_profile_id: str,
    roxy_profile_email: str,
    chat_id: str,
    logger: callable | None = None,
) -> None:
    def log(message: str) -> None:
        if logger:
            logger(message)
        else:
            print(message, flush=True)

    try:
        with AUTOMATION_LOCK:
            result = _run_automation_subprocess(
                {
                    "mode": "brand_override" if override_brand else "brand",
                    "brand": brand,
                    "adjust_brand_name": adjust_brand_name or brand,
                    "override_brand": override_brand,
                    "roxy_profile_id": roxy_profile_id,
                    "roxy_profile_email": roxy_profile_email,
                },
                logger=log,
            )
            fb_app_id = str(result.get("fb_app_id") or "")
        update_task(task_id, status="success", new_app_id=fb_app_id, last_error="")
        override_text = f" | 覆盖：{override_brand}" if override_brand else ""
        send_lark_text(chat_id, f"配置完成：{brand}{override_text} | Adjust：{adjust_brand_name or brand} | FB App ID：{fb_app_id}", logger=log)
    except Exception as exc:
        detail = exc.detail if isinstance(exc, AutomationSubprocessError) and exc.detail else traceback.format_exc()
        created_app_id = str(getattr(exc, "created_app_id", "") or "")
        cleanup_message = str(getattr(exc, "cleanup_message", "") or "")
        update_task(task_id, status="failed", new_app_id=created_app_id, last_error=detail)
        reason = compact_error_reason(detail, exc)
        cleanup_line = cleanup_message or "未创建 App，无需删除"
        log(f"[lark] failure reason: {reason}")
        log(f"[lark] cleanup result: {cleanup_line}")
        message = (
            f"配置失败：{brand}{f' | 覆盖：{override_brand}' if override_brand else ''} | Adjust：{adjust_brand_name or brand} | "
            f"原因：{reason} | "
            f"新建 App：{created_app_id or '-'} | "
            f"失败清理：{cleanup_line}"
        )
        send_lark_text(
            chat_id,
            message,
            logger=log,
        )


def run_ad_auth_task(
    app_or_brands: list[str],
    ad_account_ids: list[str],
    roxy_profile_id: str,
    command_roxy_profile_email: str,
    chat_id: str,
    logger: callable | None = None,
) -> None:
    def log(message: str) -> None:
        if logger:
            logger(message)
        else:
            print(message, flush=True)

    summary: list[str] = []
    failures: list[str] = []
    with AUTOMATION_LOCK:
        for app_or_brand in app_or_brands:
            try:
                app_id, monitor_email, source = resolve_monitor_app_and_email(app_or_brand, logger=log)
                selected_email = command_roxy_profile_email or monitor_email
                selected_profile_id = roxy_profile_id
                selected_profile_label = roxy_profile_id
                if selected_email:
                    selected_profile_id, selected_profile_label = resolve_roxy_profile_by_email(selected_email)
                elif not selected_profile_id:
                    raise RuntimeError(f"{app_or_brand} 表格没有邮箱，请先创建/同步该品牌邮箱")
                elif source.startswith("monitor:"):
                    raise RuntimeError(f"{app_or_brand} 表格邮箱为空，请先补充邮箱")
                log(f"[ad-auth] resolved {app_or_brand} => {app_id} ({source}) profile={selected_profile_label}")
                result = _run_automation_subprocess(
                    {
                        "mode": "ad_auth",
                        "app_id": app_id,
                        "ad_account_ids": ad_account_ids,
                        "roxy_profile_id": selected_profile_id,
                    },
                    logger=log,
                )
                added_ids = list(result.get("added_ids") or [])
                duplicate_ids = list(result.get("duplicate_ids") or [])
                submitted_ids = list(result.get("submitted_ids") or [])
                summary.append(
                    f"{app_or_brand}->App {app_id} 邮箱{selected_profile_label} 新增{len(added_ids)} 重复{len(duplicate_ids)} 总计{len(submitted_ids)}"
                )
            except Exception as exc:
                detail = exc.detail if isinstance(exc, AutomationSubprocessError) and exc.detail else traceback.format_exc()
                reason = compact_error_reason(detail, exc)
                log(f"[lark] ad auth failed target={app_or_brand}: {reason}")
                failures.append(f"{app_or_brand}：{reason}")

    if failures:
        completed = f" | 已完成：{'；'.join(summary)}" if summary else ""
        send_lark_text(
            chat_id,
            f"广告授权部分失败：{','.join(app_or_brands)} | 失败：{'；'.join(failures)}{completed}",
            logger=log,
        )
        return
    send_lark_text(
        chat_id,
        f"广告授权完成：{'；'.join(summary)}",
        logger=log,
    )


def run_ad_remove_task(
    app_or_brands: list[str],
    ad_account_ids: list[str],
    roxy_profile_id: str,
    command_roxy_profile_email: str,
    chat_id: str,
    logger: callable | None = None,
) -> None:
    def log(message: str) -> None:
        if logger:
            logger(message)
        else:
            print(message, flush=True)

    summary: list[str] = []
    failures: list[str] = []
    with AUTOMATION_LOCK:
        for app_or_brand in app_or_brands:
            try:
                app_id, monitor_email, source = resolve_monitor_app_and_email(app_or_brand, logger=log)
                selected_email = command_roxy_profile_email or monitor_email
                selected_profile_id = roxy_profile_id
                selected_profile_label = roxy_profile_id
                if selected_email:
                    selected_profile_id, selected_profile_label = resolve_roxy_profile_by_email(selected_email)
                elif not selected_profile_id:
                    raise RuntimeError(f"{app_or_brand} 表格没有邮箱，请先创建/同步该品牌邮箱")
                elif source.startswith("monitor:"):
                    raise RuntimeError(f"{app_or_brand} 表格邮箱为空，请先补充邮箱")
                log(f"[ad-remove] resolved {app_or_brand} => {app_id} ({source}) profile={selected_profile_label}")
                result = _run_automation_subprocess(
                    {
                        "mode": "ad_remove",
                        "app_id": app_id,
                        "ad_account_ids": ad_account_ids,
                        "roxy_profile_id": selected_profile_id,
                    },
                    logger=log,
                )
                removed_ids = list(result.get("removed_ids") or [])
                missing_ids = list(result.get("missing_ids") or [])
                submitted_ids = list(result.get("submitted_ids") or [])
                if ad_account_ids:
                    summary.append(
                        f"{app_or_brand}->App {app_id} 邮箱{selected_profile_label} 删除{len(removed_ids)} 不存在{len(missing_ids)} 剩余{len(submitted_ids)}"
                    )
                else:
                    summary.append(
                        f"{app_or_brand}->App {app_id} 邮箱{selected_profile_label} 清空{len(removed_ids)} 剩余{len(submitted_ids)}"
                    )
            except Exception as exc:
                detail = exc.detail if isinstance(exc, AutomationSubprocessError) and exc.detail else traceback.format_exc()
                reason = compact_error_reason(detail, exc)
                log(f"[lark] ad remove failed target={app_or_brand}: {reason}")
                failures.append(f"{app_or_brand}：{reason}")

    if failures:
        completed = f" | 已完成：{'；'.join(summary)}" if summary else ""
        send_lark_text(
            chat_id,
            f"广告授权删除部分失败：{','.join(app_or_brands)} | 失败：{'；'.join(failures)}{completed}",
            logger=log,
        )
        return
    send_lark_text(
        chat_id,
        f"广告授权删除完成：{'；'.join(summary)}",
        logger=log,
    )

def run_tiktok_app_id_task(
    brand_id_pairs: list[tuple[str, str]],
    chat_id: str,
    logger: callable | None = None,
) -> None:
    def log(message: str) -> None:
        if logger:
            logger(message)
        else:
            print(message, flush=True)

    from adjust_service import AdjustService

    summary: list[str] = []
    failures: list[str] = []
    brands = [brand for brand, _ in brand_id_pairs]
    try:
        service = AdjustService.from_config()
    except Exception as exc:
        detail = traceback.format_exc()
        reason = compact_error_reason(detail, exc)
        send_lark_text(chat_id, f"TikTok ID 更新失败：{','.join(brands)} | 原因：{reason}", logger=log)
        return
    for brand, tiktok_app_id in brand_id_pairs:
        try:
            result = service.ensure_tiktok_partner_app_id(brand, tiktok_app_id)
            summary.append(
                f"{result.app_name} 新增{len(result.added_ids)} 重复{len(result.duplicate_ids)} 总计{len(result.submitted_ids)}"
            )
        except Exception as exc:
            detail = traceback.format_exc()
            reason = compact_error_reason(detail, exc)
            log(f"[lark] tt failed target={brand}: {reason}")
            failures.append(f"{brand}：{reason}")

    if failures:
        completed = f" | 已完成：{'；'.join(summary)}" if summary else ""
        send_lark_text(
            chat_id,
            f"TikTok ID 更新部分失败：{','.join(brands)} | 失败：{'；'.join(failures)}{completed}",
            logger=log,
        )
        return
    send_lark_text(
        chat_id,
        f"更新完成：{'；'.join(summary)}",
        logger=log,
    )


def ensure_serial_worker(logger: callable | None = None) -> None:
    global _SERIAL_WORKER_STARTED
    with _SERIAL_WORKER_LOCK:
        if _SERIAL_WORKER_STARTED:
            return
        thread = threading.Thread(target=_serial_worker_loop, args=(logger,), daemon=True)
        thread.start()
        _SERIAL_WORKER_STARTED = True
    if logger:
        logger("[worker] serial automation worker started")


def enqueue_brand_queue_drain(logger: callable | None = None) -> None:
    ensure_serial_worker(logger)
    _SERIAL_WORK_QUEUE.put({"kind": "drain_brand_tasks"})


def enqueue_brand_work(task_id: str, logger: callable | None = None) -> None:
    ensure_serial_worker(logger)
    _SERIAL_WORK_QUEUE.put({"kind": "brand_task", "task_id": task_id})


def enqueue_ad_auth_work(
    app_or_brands: list[str],
    ad_account_ids: list[str],
    roxy_profile_id: str,
    command_roxy_profile_email: str,
    chat_id: str,
    logger: callable | None = None,
) -> None:
    ensure_serial_worker(logger)
    _SERIAL_WORK_QUEUE.put(
        {
            "kind": "ad_auth",
            "app_or_brands": app_or_brands,
            "ad_account_ids": ad_account_ids,
            "roxy_profile_id": roxy_profile_id,
            "command_roxy_profile_email": command_roxy_profile_email,
            "chat_id": chat_id,
        }
    )


def enqueue_ad_remove_work(
    app_or_brands: list[str],
    ad_account_ids: list[str],
    roxy_profile_id: str,
    command_roxy_profile_email: str,
    chat_id: str,
    logger: callable | None = None,
) -> None:
    ensure_serial_worker(logger)
    _SERIAL_WORK_QUEUE.put(
        {
            "kind": "ad_remove",
            "app_or_brands": app_or_brands,
            "ad_account_ids": ad_account_ids,
            "roxy_profile_id": roxy_profile_id,
            "command_roxy_profile_email": command_roxy_profile_email,
            "chat_id": chat_id,
        }
    )


def enqueue_tiktok_app_id_work(
    brand_id_pairs: list[tuple[str, str]],
    chat_id: str,
    logger: callable | None = None,
) -> None:
    ensure_serial_worker(logger)
    _SERIAL_WORK_QUEUE.put(
        {
            "kind": "tiktok_app_id",
            "brand_id_pairs": brand_id_pairs,
            "chat_id": chat_id,
        }
    )


def _serial_worker_loop(logger: callable | None = None) -> None:
    def log(message: str) -> None:
        if logger:
            logger(message)
        else:
            print(message, flush=True)

    while True:
        work = _SERIAL_WORK_QUEUE.get()
        try:
            assert_not_expired()
            kind = str(work.get("kind") or "")
            if kind == "drain_brand_tasks":
                _drain_brand_tasks(log)
            elif kind == "brand_task":
                _run_claimed_brand_task(str(work.get("task_id") or ""), log)
            elif kind == "ad_auth":
                run_ad_auth_task(
                    list(work.get("app_or_brands") or []),
                    list(work.get("ad_account_ids") or []),
                    str(work.get("roxy_profile_id") or ""),
                    str(work.get("command_roxy_profile_email") or ""),
                    str(work.get("chat_id") or ""),
                    logger=log,
                )
            elif kind == "ad_remove":
                run_ad_remove_task(
                    list(work.get("app_or_brands") or []),
                    list(work.get("ad_account_ids") or []),
                    str(work.get("roxy_profile_id") or ""),
                    str(work.get("command_roxy_profile_email") or ""),
                    str(work.get("chat_id") or ""),
                    logger=log,
                )
            elif kind == "tiktok_app_id":
                brand_id_pairs = [
                    (str(item[0]), str(item[1]))
                    for item in list(work.get("brand_id_pairs") or [])
                    if isinstance(item, (list, tuple)) and len(item) >= 2
                ]
                run_tiktok_app_id_task(
                    brand_id_pairs,
                    str(work.get("chat_id") or ""),
                    logger=log,
                )
            else:
                log(f"[worker] unknown queue item: {work}")
        except Exception:
            log(f"[worker] serial worker error:\n{traceback.format_exc()}")
        finally:
            _SERIAL_WORK_QUEUE.task_done()


def _drain_brand_tasks(logger: callable) -> None:
    while True:
        task = claim_next_task()
        if not task:
            return
        _run_brand_task_object(task, logger)


def _run_claimed_brand_task(task_id: str, logger: callable) -> None:
    task = claim_task(task_id)
    if not task:
        logger(f"[worker] queued config task is no longer pending: {task_id}")
        return
    _run_brand_task_object(task, logger)


def _run_brand_task_object(task: Any, logger: callable) -> None:
    logger(f"[worker] running queued config task: {task.brand} ({task.task_id})")
    run_brand_task(
        task.task_id,
        task.brand,
        task.adjust_brand_name or task.brand,
        task.override_brand,
        task.roxy_profile_id,
        task.roxy_profile_email,
        task.chat_id,
        logger=logger,
    )


def handle_event(
    event: dict[str, Any],
    roxy_profile_id: str,
    worker_name: str = "",
    logger: callable | None = None,
) -> None:
    def log(message: str) -> None:
        if logger:
            logger(message)
        else:
            print(message, flush=True)

    raw_content = str(event.get("content") or "").strip()
    content = clean_command_content(raw_content)
    chat_id = str(event.get("chat_id") or "")
    sender_id = str(event.get("sender_id") or "")
    event_id = str(event.get("event_id") or "")

    if not content:
        return

    if content:
        try:
            assert_not_expired()
        except Exception:
            send_lark_text(chat_id, EXPIRED_MESSAGE, logger=log)
            return

    state = load_state()
    processed = set(state.get("processed_event_ids", []))
    if event_id and event_id in processed:
        return
    if should_ignore_historical_event(event, state):
        remember_processed_event(state, event_id)
        save_state(state)
        log(f"[lark] ignored historical event: {event_id or '-'}")
        return

    if is_profile_list_command(content):
        log(f"[lark] command received: profile list")
        remember_processed_event(state, event_id)
        save_state(state)
        send_profile_list(chat_id, selected_profile_id=roxy_profile_id, logger=log)
        return

    tiktok_app_id_command = parse_tiktok_app_id_command(content)
    if tiktok_app_id_command:
        brand_id_pairs = tiktok_app_id_command
        brands = [brand for brand, _ in brand_id_pairs]
        log(f"[lark] command received: tt {','.join(brands)} groups={len(brand_id_pairs)}")
        remember_processed_event(state, event_id)
        save_state(state)
        enqueue_tiktok_app_id_work(brand_id_pairs, chat_id, logger)
        send_lark_text(
            chat_id,
            f"已受理品牌：{','.join(brands)}",
            logger=log,
        )
        return
    if TT_PATTERN.search(content):
        remember_processed_event(state, event_id)
        save_state(state)
        send_lark_text(
            chat_id,
            "TikTok ID 命令格式错误：请使用 tt\\nqstand250\\n123\\nqstand251\\n456",
            logger=log,
        )
        return

    ad_remove_all = parse_ad_remove_all_command(content)
    if ad_remove_all:
        app_or_brands, command_roxy_profile_email = ad_remove_all
        log(f"[lark] command received: ad remove all {','.join(app_or_brands)}")
        remember_processed_event(state, event_id)
        save_state(state)
        command_roxy_profile_label = ""
        if command_roxy_profile_email:
            try:
                _, command_roxy_profile_label = resolve_roxy_profile_by_email(command_roxy_profile_email)
            except Exception as exc:
                send_lark_text(chat_id, f"广告授权清空失败：{','.join(app_or_brands)} | 原因：{exc}", logger=log)
                return
        enqueue_ad_remove_work(app_or_brands, [], roxy_profile_id, command_roxy_profile_email, chat_id, logger)
        profile_text = command_roxy_profile_label or "按表格邮箱"
        send_lark_text(
            chat_id,
            f"已受理广告授权清空：{','.join(app_or_brands)} | Roxy 邮箱: {profile_text}",
            logger=log,
        )
        return

    ad_remove = parse_ad_remove_command(content)
    if ad_remove:
        app_or_brands, ad_account_ids, command_roxy_profile_email = ad_remove
        log(f"[lark] command received: ad remove {','.join(app_or_brands)} ids={len(ad_account_ids)}")
        remember_processed_event(state, event_id)
        save_state(state)
        command_roxy_profile_label = ""
        if command_roxy_profile_email:
            try:
                _, command_roxy_profile_label = resolve_roxy_profile_by_email(command_roxy_profile_email)
            except Exception as exc:
                send_lark_text(chat_id, f"广告授权删除失败：{','.join(app_or_brands)} | 原因：{exc}", logger=log)
                return
        enqueue_ad_remove_work(app_or_brands, ad_account_ids, roxy_profile_id, command_roxy_profile_email, chat_id, logger)
        profile_text = command_roxy_profile_label or "按表格邮箱"
        send_lark_text(
            chat_id,
            f"已受理广告授权删除：{','.join(app_or_brands)} | 广告账号：{len(ad_account_ids)} 个 | Roxy 邮箱: {profile_text}",
            logger=log,
        )
        return
    if AD_REMOVE_PATTERN.search(content):
        remember_processed_event(state, event_id)
        save_state(state)
        send_lark_text(
            chat_id,
            "广告授权删除命令格式错误：请在删除目标后填写广告账号 ID，例如：删除 qstand231 邮箱\\n1714489949761975",
            logger=log,
        )
        return

    ad_auth = parse_ad_auth_command(content)
    if ad_auth:
        app_or_brands, ad_account_ids, command_roxy_profile_email = ad_auth
        log(f"[lark] command received: ad auth {','.join(app_or_brands)} ids={len(ad_account_ids)}")
        remember_processed_event(state, event_id)
        save_state(state)
        command_roxy_profile_label = ""
        if command_roxy_profile_email:
            try:
                _, command_roxy_profile_label = resolve_roxy_profile_by_email(command_roxy_profile_email)
            except Exception as exc:
                send_lark_text(chat_id, f"广告授权失败：{','.join(app_or_brands)} | 原因：{exc}", logger=log)
                return
        enqueue_ad_auth_work(app_or_brands, ad_account_ids, roxy_profile_id, command_roxy_profile_email, chat_id, logger)
        profile_text = command_roxy_profile_label or "按表格邮箱"
        send_lark_text(
            chat_id,
            f"已受理广告授权：{','.join(app_or_brands)} | 广告账号：{len(ad_account_ids)} 个 | Roxy 邮箱: {profile_text}",
            logger=log,
        )
        return
    if AD_AUTH_PATTERN.search(content):
        remember_processed_event(state, event_id)
        save_state(state)
        send_lark_text(
            chat_id,
            "广告授权命令格式错误：请在授权目标后填写广告账号 ID，例如：授权 qstand231 邮箱\\n1714489949761975",
            logger=log,
        )
        return

    override_parsed = parse_config_override_command(content)
    if override_parsed:
        brand, override_brand, command_roxy_profile_email = override_parsed
        log(f"[lark] command received: configure override {brand} covers {override_brand}")
        command_roxy_profile_id = ""
        command_roxy_profile_label = ""
        if command_roxy_profile_email:
            try:
                command_roxy_profile_id, command_roxy_profile_label = resolve_roxy_profile_by_email(command_roxy_profile_email)
            except Exception as exc:
                remember_processed_event(state, event_id)
                save_state(state)
                send_lark_text(chat_id, f"配置失败：{brand} 覆盖 {override_brand} | 原因：{exc}", logger=log)
                return
        effective_roxy_profile_id = command_roxy_profile_id or roxy_profile_id
        effective_roxy_profile_label = command_roxy_profile_label
        if not effective_roxy_profile_label and effective_roxy_profile_id:
            try:
                effective_roxy_profile_label = resolve_roxy_profile_email_by_id(effective_roxy_profile_id)
            except Exception as exc:
                log(f"[lark] default roxy email lookup failed: {exc}")
        if not effective_roxy_profile_id:
            remember_processed_event(state, event_id)
            save_state(state)
            send_lark_text(chat_id, f"配置失败：{brand} 覆盖 {override_brand} | 原因：未选择默认 Roxy 账号；请在工具里选择账号，或使用：配置 {brand} 覆盖 {override_brand} email@example.com", logger=log)
            return
        if not effective_roxy_profile_label:
            remember_processed_event(state, event_id)
            save_state(state)
            send_lark_text(chat_id, f"配置失败：{brand} 覆盖 {override_brand} | 原因：默认 Roxy 账号未检测到邮箱；请发送“查看邮箱”确认，或使用：配置 {brand} 覆盖 {override_brand} email@example.com", logger=log)
            return

        task, created = enqueue_brand_task(
            brand,
            adjust_brand_name=brand,
            override_brand=override_brand,
            reason="lark_override",
            roxy_profile_id=effective_roxy_profile_id,
            roxy_profile_email=effective_roxy_profile_label,
            chat_id=chat_id,
            sender_id=sender_id,
        )
        remember_processed_event(state, event_id)
        save_state(state)
        if created or task.status == "pending":
            enqueue_brand_work(task.task_id, logger)

        verb = "已受理覆盖配置" if created else "已有覆盖任务"
        send_lark_text(
            chat_id,
            f"{verb}：{task.brand}\n覆盖：{task.override_brand}\nAdjust：{task.adjust_brand_name or task.brand}\n任务ID: {task.task_id}\nRoxy 邮箱: {effective_roxy_profile_label or '-'}\n发送人: {sender_id or '-'}",
            logger=log,
        )
        return

    parsed = parse_command(content)
    if not parsed:
        return
    brand, adjust_brand_name, command_worker_name, command_roxy_profile_email = parsed
    log(f"[lark] command received: configure {brand}")
    if worker_name and command_worker_name and command_worker_name.lower() != worker_name.lower():
        log(f"忽略其他 Worker 命令：target={command_worker_name}, local={worker_name}, content={content}")
        return
    command_roxy_profile_id = ""
    command_roxy_profile_label = ""
    if command_roxy_profile_email:
        try:
            command_roxy_profile_id, command_roxy_profile_label = resolve_roxy_profile_by_email(command_roxy_profile_email)
        except Exception as exc:
            remember_processed_event(state, event_id)
            save_state(state)
            send_lark_text(chat_id, f"配置失败：{brand} | 原因：{exc}", logger=log)
            return
    effective_roxy_profile_id = command_roxy_profile_id or roxy_profile_id
    effective_roxy_profile_label = command_roxy_profile_label
    if not effective_roxy_profile_label and effective_roxy_profile_id:
        try:
            effective_roxy_profile_label = resolve_roxy_profile_email_by_id(effective_roxy_profile_id)
        except Exception as exc:
            log(f"[lark] default roxy email lookup failed: {exc}")
    if not effective_roxy_profile_id:
        remember_processed_event(state, event_id)
        save_state(state)
        send_lark_text(chat_id, f"配置失败：{brand} | 原因：未选择默认 Roxy 账号；请在工具里选择账号，或使用：配置 {brand} email@example.com", logger=log)
        return
    if not effective_roxy_profile_label:
        remember_processed_event(state, event_id)
        save_state(state)
        send_lark_text(chat_id, f"配置失败：{brand} | 原因：默认 Roxy 账号未检测到邮箱；请发送“查看邮箱”确认，或使用：配置 {brand} email@example.com", logger=log)
        return

    task, created = enqueue_brand_task(
        brand,
        adjust_brand_name=adjust_brand_name,
        roxy_profile_id=effective_roxy_profile_id,
        roxy_profile_email=effective_roxy_profile_label,
        chat_id=chat_id,
        sender_id=sender_id,
    )
    remember_processed_event(state, event_id)
    save_state(state)
    if created or task.status == "pending":
        enqueue_brand_work(task.task_id, logger)

    verb = "已受理" if created else "已有任务"
    send_lark_text(
        chat_id,
        f"{verb}：{task.brand}\nAdjust：{task.adjust_brand_name or task.brand}\n任务ID: {task.task_id}\nRoxy 邮箱: {effective_roxy_profile_label or '-'}\n发送人: {sender_id or '-'}",
        logger=log,
    )


def _sdk_event_source_command(seconds: int) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--lark-sdk-event-source", "--seconds", str(seconds)]
    return [sys.executable, str(Path(__file__).with_name("lark_sdk_event_source.py")), "--seconds", str(seconds)]


def _consume_event_process(
    command: list[str],
    seconds: int,
    roxy_profile_id: str,
    worker_name: str = "",
    logger: callable | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    child_env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
    )

    def log_stderr() -> None:
        for line in iter(process.stderr.readline, ""):
            text = line.rstrip()
            if logger:
                logger(text)
            else:
                print(text, file=sys.stderr, flush=True)

    stderr_thread = threading.Thread(target=log_stderr, daemon=True)
    stderr_thread.start()

    start_time = time.time()
    try:
        while time.time() - start_time < seconds:
            if stop_event and stop_event.is_set():
                break
            line = process.stdout.readline()
            if not line:
                if process.poll() is not None:
                    break
                time.sleep(0.2)
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            handle_event(event, roxy_profile_id, worker_name=worker_name, logger=logger)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
    return process.returncode or 0


def _consume_events_cli(
    seconds: int,
    roxy_profile_id: str,
    worker_name: str = "",
    logger: callable | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    cli_path = lark_cli_path()
    ensure_lark_cli_configured(cli_path, logger)
    command = [
        cli_path,
        "event",
        "consume",
        "im.message.receive_v1",
        "--timeout",
        f"{seconds}s",
        "--as",
        "bot",
    ]
    return _consume_event_process(command, seconds, roxy_profile_id, worker_name, logger, stop_event)


def consume_events(
    seconds: int,
    roxy_profile_id: str,
    worker_name: str = "",
    logger: callable | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    assert_not_expired()
    with lark_listener_lock():
        state = load_state()
        state["listener_started_at_ms"] = int(time.time() * 1000)
        save_state(state)
        enqueue_brand_queue_drain(logger)

        app_id, app_secret = _lark_config()
        if logger:
            safe_app = f"{app_id[:8]}..." if app_id else "-"
            logger(f"[lark] listener lock acquired; app={safe_app}")

        prefer_sdk = prefer_sdk_listener()

        def run_sdk_listener() -> int | None:
            if not app_id or not app_secret:
                return None
            if logger:
                logger("[lark] using built-in SDK event listener")
            code = _consume_event_process(
                _sdk_event_source_command(seconds),
                seconds,
                roxy_profile_id,
                worker_name,
                logger,
                stop_event,
            )
            if code == 0 or (stop_event and stop_event.is_set()):
                return code
            if logger:
                logger(f"[lark] SDK listener exited code={code}; fallback to lark-cli")
            return None

        def run_cli_listener() -> int | None:
            try:
                cli_path = lark_cli_path()
            except FileNotFoundError:
                return None
            if logger:
                logger(f"[lark] using lark-cli event listener: {cli_path}")
            code = _consume_events_cli(seconds, roxy_profile_id, worker_name, logger, stop_event)
            if code == 0 or (stop_event and stop_event.is_set()):
                return code
            if logger:
                logger(f"[lark] lark-cli listener exited code={code}; fallback to SDK")
            return None

        first = run_sdk_listener if prefer_sdk else run_cli_listener
        second = run_cli_listener if prefer_sdk else run_sdk_listener

        code = first()
        if code is not None:
            return code
        code = second()
        if code is not None:
            return code

        raise RuntimeError("Lark 监听不可用：请配置 lark.app_id/lark.app_secret，或安装 lark-cli。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Listen Lark commands and run Facebook automation.")
    parser.add_argument("--roxy-profile-id", default="", help="Roxy profile dirId used to run Facebook automation.")
    parser.add_argument("--worker-name", default="", help="Only run commands targeting this worker name.")
    parser.add_argument("--seconds", type=int, default=3600)
    return parser


def main() -> None:
    assert_not_expired()
    args = build_parser().parse_args()
    if args.worker_name:
        print(f"[listener] worker_name={args.worker_name}", flush=True)
    if args.roxy_profile_id:
        print(f"[listener] auto-run enabled, roxy_profile_id={args.roxy_profile_id}", flush=True)
    else:
        print("[listener] no default roxy profile. Use message format: 配置 qstand237 email@example.com.", flush=True)
    consume_events(seconds=args.seconds, roxy_profile_id=args.roxy_profile_id, worker_name=args.worker_name)


if __name__ == "__main__":
    main()
