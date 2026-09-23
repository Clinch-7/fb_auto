from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from config import project_dir


CONFIG_FILE_NAME = "app_config.json"


DEFAULT_APP_CONFIG: dict[str, Any] = {
    "roxy": {
        "api_host": "http://127.0.0.1:50000",
        "api_key": "",
        "workspace_id": None,
    },
    "vest": {
        "ops_login_url": "https://ga-client.ops-center.net/api/user/login/",
        "vest_login_url": "https://api-vest.tgcgame.com/admin/login_by_token/",
        "vest_api_base": "https://api-vest.tgcgame2026.com",
        "organization": "Quest",
        "ops_username": "",
        "ops_password": "",
        "ops_totp_secret": "",
    },
    "lark": {
        "openapi_base": "https://open.larksuite.com",
        "app_id": "",
        "app_secret": "",
        "bot_name": "",
    },
    "monitor_sheet": {
        "openapi_base": "",
        "app_id": "",
        "app_secret": "",
        "spreadsheet_token": "",
        "sheet_id": "",
    },
    "adjust": {
        "api_token": "",
        "api_base": "https://api.adjust.com/dashboard/api",
        "automation_api_base": "https://automate.adjust.com/app-automation",
        "enabled": True,
        "facebook_event_mappings": {
            "Register": "fb_mobile_complete_registration",
            "Small": "fb_mobile_purchase",
            "Medium": "fb_mobile_purchase",
            "Large": "fb_mobile_purchase",
        },
        "tiktok_event_mappings": {
            "Register": "register",
            "Small": "pay",
            "Medium": "pay",
            "Large": "pay",
        },
    },
}


class AppConfigError(RuntimeError):
    pass


def app_config_path() -> Path:
    return project_dir() / CONFIG_FILE_NAME


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def ensure_app_config_exists() -> Path:
    path = app_config_path()
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(DEFAULT_APP_CONFIG, f, ensure_ascii=False, indent=2)
    return path


def load_app_config(create_if_missing: bool = True) -> dict[str, Any]:
    path = app_config_path()
    if not path.exists():
        if create_if_missing:
            ensure_app_config_exists()
        else:
            return deepcopy(DEFAULT_APP_CONFIG)

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise AppConfigError(f"配置文件 JSON 格式错误: {path}") from exc

    if not isinstance(data, dict):
        raise AppConfigError(f"配置文件必须是 JSON object: {path}")
    return _deep_merge(DEFAULT_APP_CONFIG, data)


def save_app_config(config: dict[str, Any]) -> Path:
    merged = _deep_merge(DEFAULT_APP_CONFIG, config)
    path = app_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    tmp.replace(path)
    return path


def section(name: str) -> dict[str, Any]:
    data = load_app_config()
    value = data.get(name, {})
    return value if isinstance(value, dict) else {}


def value(section_name: str, key: str, default: Any = "") -> Any:
    env_key = f"FB_AUTO_{section_name}_{key}".upper()
    if env_key in os.environ:
        return os.environ[env_key]
    return section(section_name).get(key, default)


def required(section_name: str, key: str, label: str | None = None) -> str:
    raw = value(section_name, key, "")
    text = str(raw or "").strip()
    if not text:
        raise AppConfigError(f"请先在 {app_config_path()} 配置 {label or f'{section_name}.{key}'}")
    return text
