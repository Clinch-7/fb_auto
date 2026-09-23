from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from config import runtime_dir
except ImportError:
    def runtime_dir() -> Path:
        path = Path(__file__).resolve().parent / "runtime"
        path.mkdir(parents=True, exist_ok=True)
        return path


TASK_TYPE_RECREATE = "facebook_app_recreate"
ACTIVE_STATUSES = {"pending", "running", "need_manual"}
TERMINAL_STATUSES = {"success", "failed", "cancelled"}
DEFAULT_MAX_ATTEMPTS = 3
STALE_ACTIVE_SECONDS = 20 * 60


@dataclass
class RepairTask:
    task_id: str
    task_type: str
    brand: str
    monitor_name: str
    adjust_brand_name: str = ""
    override_brand: str = ""
    old_app_id: str = ""
    old_app_secret: str = ""
    source_row: int | None = None
    reason: str = ""
    error_code: str = ""
    error_message: str = ""
    status: str = "pending"
    attempts: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    created_at: str = ""
    updated_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    locked_at: str = ""
    new_app_id: str = ""
    roxy_profile_id: str = ""
    roxy_profile_email: str = ""
    chat_id: str = ""
    sender_id: str = ""
    last_error: str = ""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _timestamp(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def _is_stale_active(task: "RepairTask") -> bool:
    if task.status not in {"running", "need_manual"}:
        return False
    started = _timestamp(task.locked_at or task.started_at or task.updated_at)
    return bool(started and time.time() - started > STALE_ACTIVE_SECONDS)


def _requeue_stale_active(task: "RepairTask") -> None:
    task.status = "pending"
    task.locked_at = ""
    task.started_at = ""
    task.updated_at = now_iso()
    task.last_error = "requeued stale active task"


def task_queue_path() -> Path:
    path = runtime_dir() / "repair_tasks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def normalize_brand(value: str) -> str:
    return str(value or "").strip()


def monitor_name_for_brand(brand: str) -> str:
    return str(brand or "").strip()


def load_tasks(path: Path | None = None) -> list[RepairTask]:
    target = path or task_queue_path()
    if not target.exists() or target.stat().st_size == 0:
        return []
    with target.open("r", encoding="utf-8-sig") as f:
        raw = json.load(f)
    rows = raw.get("tasks", raw) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    tasks: list[RepairTask] = []
    field_names = set(RepairTask.__dataclass_fields__)
    for row in rows:
        if not isinstance(row, dict):
            continue
        clean = {key: row.get(key) for key in field_names if key in row}
        try:
            tasks.append(RepairTask(**clean))
        except TypeError:
            continue
    return tasks


def save_tasks(tasks: list[RepairTask], path: Path | None = None) -> None:
    target = path or task_queue_path()
    tmp = target.with_suffix(target.suffix + ".tmp")
    payload = {"tasks": [asdict(task) for task in tasks]}
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(target)


def enqueue_recreate_task(app: dict[str, Any], path: Path | None = None) -> tuple[RepairTask, bool]:
    tasks = load_tasks(path)
    raw_name = str(app.get("name") or "").strip()
    brand = normalize_brand(raw_name)
    monitor_name = monitor_name_for_brand(raw_name or brand)
    adjust_brand_name = str(app.get("adjust_brand_name") or app.get("name") or monitor_name).strip()
    old_app_id = str(app.get("app_id") or "").strip()

    for task in tasks:
        if (
            task.task_type == TASK_TYPE_RECREATE
            and task.brand == brand
            and task.adjust_brand_name == adjust_brand_name
            and task.old_app_id == old_app_id
            and task.status in ACTIVE_STATUSES
        ):
            task.updated_at = now_iso()
            save_tasks(tasks, path)
            return task, False

    created_at = now_iso()
    task = RepairTask(
        task_id=uuid.uuid4().hex,
        task_type=TASK_TYPE_RECREATE,
        brand=brand,
        monitor_name=monitor_name,
        adjust_brand_name=adjust_brand_name,
        old_app_id=old_app_id,
        old_app_secret=str(app.get("app_secret") or "").strip(),
        source_row=app.get("row"),
        reason="app_offline",
        error_code=str(app.get("error_code") or ""),
        error_message=str(app.get("error_message") or ""),
        status="pending",
        created_at=created_at,
        updated_at=created_at,
    )
    tasks.append(task)
    save_tasks(tasks, path)
    return task, True


def enqueue_brand_task(
    brand: str,
    *,
    adjust_brand_name: str = "",
    override_brand: str = "",
    reason: str = "lark_command",
    roxy_profile_id: str = "",
    roxy_profile_email: str = "",
    chat_id: str = "",
    sender_id: str = "",
    path: Path | None = None,
) -> tuple[RepairTask, bool]:
    tasks = load_tasks(path)
    normalized_brand = normalize_brand(brand)
    normalized_adjust_brand = str(adjust_brand_name or brand).strip()
    normalized_override_brand = normalize_brand(override_brand)
    monitor_name = monitor_name_for_brand(normalized_adjust_brand)

    for task in tasks:
        if (
            task.task_type == TASK_TYPE_RECREATE
            and task.brand == normalized_brand
            and task.adjust_brand_name == normalized_adjust_brand
            and task.override_brand == normalized_override_brand
            and task.reason == reason
            and task.status in ACTIVE_STATUSES
        ):
            if _is_stale_active(task):
                _requeue_stale_active(task)
            task.updated_at = now_iso()
            if roxy_profile_id:
                task.roxy_profile_id = roxy_profile_id
            if roxy_profile_email:
                task.roxy_profile_email = roxy_profile_email
            if chat_id:
                task.chat_id = chat_id
            if sender_id:
                task.sender_id = sender_id
            save_tasks(tasks, path)
            return task, False

    created_at = now_iso()
    task = RepairTask(
        task_id=uuid.uuid4().hex,
        task_type=TASK_TYPE_RECREATE,
        brand=normalized_brand,
        monitor_name=monitor_name,
        adjust_brand_name=normalized_adjust_brand,
        override_brand=normalized_override_brand,
        reason=reason,
        status="pending",
        roxy_profile_id=roxy_profile_id,
        roxy_profile_email=roxy_profile_email,
        chat_id=chat_id,
        sender_id=sender_id,
        created_at=created_at,
        updated_at=created_at,
    )
    tasks.append(task)
    save_tasks(tasks, path)
    return task, True


def claim_next_task(roxy_profile_id: str = "", path: Path | None = None) -> RepairTask | None:
    tasks = load_tasks(path)
    for task in tasks:
        if _is_stale_active(task):
            _requeue_stale_active(task)
        if task.status != "pending":
            continue
        if task.attempts >= task.max_attempts:
            task.status = "failed"
            task.last_error = "超过最大重试次数"
            task.updated_at = now_iso()
            continue
        task.status = "running"
        task.attempts += 1
        task.started_at = now_iso()
        task.updated_at = task.started_at
        task.locked_at = task.started_at
        if roxy_profile_id:
            task.roxy_profile_id = roxy_profile_id
        save_tasks(tasks, path)
        return task
    save_tasks(tasks, path)
    return None


def claim_task(task_id: str, roxy_profile_id: str = "", path: Path | None = None) -> RepairTask | None:
    tasks = load_tasks(path)
    target = str(task_id or "").strip()
    if not target:
        return None
    for task in tasks:
        if task.task_id != target:
            continue
        if task.status != "pending":
            if _is_stale_active(task):
                _requeue_stale_active(task)
            else:
                return None
        if task.attempts >= task.max_attempts:
            task.status = "failed"
            task.last_error = "exceeded max attempts"
            task.updated_at = now_iso()
            save_tasks(tasks, path)
            return None
        task.status = "running"
        task.attempts += 1
        task.started_at = now_iso()
        task.updated_at = task.started_at
        task.locked_at = task.started_at
        if roxy_profile_id:
            task.roxy_profile_id = roxy_profile_id
        save_tasks(tasks, path)
        return task
    return None


def update_task(task_id: str, **updates: Any) -> RepairTask:
    tasks = load_tasks()
    for task in tasks:
        if task.task_id != task_id:
            continue
        for key, value in updates.items():
            if hasattr(task, key):
                setattr(task, key, value)
        task.updated_at = now_iso()
        if task.status in TERMINAL_STATUSES and not task.finished_at:
            task.finished_at = task.updated_at
        save_tasks(tasks)
        return task
    raise KeyError(f"task not found: {task_id}")
