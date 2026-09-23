from __future__ import annotations

import os
import sys
from pathlib import Path


APP_NAME = "Facebook_Auto_Hybrid"
DEFAULT_BROWSER = "roxy"


def project_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def runtime_dir() -> Path:
    path = project_dir() / "runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def profile_root() -> Path:
    path = runtime_dir() / "browser_profiles"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    path = runtime_dir() / "request_logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cookie_path(browser: str, account: str | None = None) -> Path:
    suffix = f"_{account}" if account else ""
    return runtime_dir() / f"fb_cookies_{browser}{suffix}.json"


def chrome_path() -> str:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError("未找到 Google Chrome 安装路径")


def edge_path() -> str:
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError("未找到 Microsoft Edge 安装路径")
