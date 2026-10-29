from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from app_config import load_app_config, save_app_config


DEFAULT_ROXY_API_HOST = "http://127.0.0.1:50000"


@dataclass(frozen=True)
class RoxySettings:
    api_host: str = DEFAULT_ROXY_API_HOST
    api_key: str = ""
    workspace_id: int | None = None


@dataclass(frozen=True)
class RoxyProfile:
    dir_id: str
    name: str
    user_name: str = ""
    sort_num: str = ""
    remark: str = field(default="", repr=False)
    open_status: bool = False
    workspace_id: int | None = None
    workspace_name: str = ""

    @property
    def confirmation_password(self) -> str:
        lines = self.remark.splitlines()
        return lines[0].strip() if lines else ""

    @property
    def display_name(self) -> str:
        parts = [part for part in (self.sort_num, self.name, self.workspace_name) if part]
        text = " / ".join(parts) if parts else self.dir_id
        return f"{text} ({self.dir_id[:8]})"


def load_roxy_settings() -> RoxySettings:
    data = load_app_config().get("roxy", {})
    if not isinstance(data, dict):
        data = {}

    api_host = (
        os.getenv("ROXY_API_HOST")
        or str(data.get("api_host") or data.get("host") or DEFAULT_ROXY_API_HOST)
    ).strip()
    api_key = (os.getenv("ROXY_API_KEY") or str(data.get("api_key") or data.get("token") or "")).strip()

    workspace_value = os.getenv("ROXY_WORKSPACE_ID") or data.get("workspace_id")
    workspace_id = None
    if workspace_value not in (None, ""):
        try:
            workspace_id = int(workspace_value)
        except (TypeError, ValueError):
            workspace_id = None

    return RoxySettings(api_host=api_host.rstrip("/"), api_key=api_key, workspace_id=workspace_id)


def save_roxy_settings(settings: RoxySettings):
    config = load_app_config()
    config["roxy"] = {
        "api_host": settings.api_host.rstrip("/") or DEFAULT_ROXY_API_HOST,
        "api_key": settings.api_key,
        "workspace_id": settings.workspace_id,
    }
    return save_app_config(config)


class RoxyApiError(RuntimeError):
    pass


class RoxyClient:
    def __init__(self, settings: RoxySettings | None = None, timeout: int = 30) -> None:
        self.settings = settings or load_roxy_settings()
        self.timeout = timeout

    @property
    def api_host(self) -> str:
        return self.settings.api_host.rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["token"] = self.settings.api_key
            headers["x-api-key"] = self.settings.api_key
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        return headers

    def _request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> dict:
        url = f"{self.api_host}{path}"
        try:
            response = requests.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RoxyApiError(f"Roxy API 连接失败：{exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise RoxyApiError(f"Roxy API 非 JSON 响应：HTTP {response.status_code} {response.text[:300]}") from exc

        if response.status_code >= 400:
            raise RoxyApiError(f"Roxy API HTTP {response.status_code}: {data}")
        return data

    @staticmethod
    def _ensure_ok(data: dict, action: str) -> dict:
        if data.get("code") == 0:
            return data
        raise RoxyApiError(f"{action} 失败：{data.get('msg') or data}")

    def health(self) -> bool:
        data = self._request("GET", "/health")
        self._ensure_ok(data, "Roxy health")
        return True

    def workspaces(self) -> list[dict]:
        data = self._request("GET", "/browser/workspace")
        self._ensure_ok(data, "读取 Roxy 工作区")
        return data.get("data", {}).get("rows", []) or []

    def workspace_id(self) -> int:
        if self.settings.workspace_id:
            return self.settings.workspace_id

        rows = self.workspaces()
        if not rows:
            raise RoxyApiError("Roxy 没有返回任何工作区。")
        return int(rows[0]["id"])

    def list_profiles(self, page_size: int = 100) -> list[RoxyProfile]:
        workspace_rows = (
            [{"id": self.settings.workspace_id, "workspaceName": ""}]
            if self.settings.workspace_id
            else self.workspaces()
        )
        profiles: list[RoxyProfile] = []

        for workspace in workspace_rows:
            workspace_id = int(workspace["id"])
            workspace_name = str(workspace.get("workspaceName") or "").strip()
            page_index = 1

            while True:
                data = self._request(
                    "GET",
                    "/browser/list_v3",
                    params={"workspaceId": workspace_id, "page_index": page_index, "page_size": page_size},
                )
                self._ensure_ok(data, "读取 Roxy Profile")
                payload = data.get("data", {})
                rows = payload.get("rows", []) or []

                for row in rows:
                    dir_id = str(row.get("dirId") or "").strip()
                    if not dir_id:
                        continue
                    profiles.append(
                        RoxyProfile(
                            dir_id=dir_id,
                            name=str(row.get("windowName") or "").strip(),
                            user_name=str(row.get("userName") or "").strip(),
                            sort_num=str(row.get("windowSortNum") or "").strip(),
                            remark=str(row.get("windowRemark") or ""),
                            open_status=bool(row.get("openStatus")),
                            workspace_id=workspace_id,
                            workspace_name=str(row.get("workspaceName") or workspace_name).strip(),
                        )
                    )

                total = int(payload.get("total") or 0)
                if page_index * page_size >= total or not rows:
                    break
                page_index += 1

        return profiles

    def find_profile(self, dir_id: str) -> RoxyProfile | None:
        target = str(dir_id).strip()
        for profile in self.list_profiles():
            if profile.dir_id == target:
                return profile
        return None

    def open_profile(self, dir_id: str, args: list[str] | None = None) -> dict:
        data = self._request("POST", "/browser/open", json_body={"dirId": dir_id, "args": args or []})
        if data.get("code") == 0:
            return data.get("data") or {}

        info = self.connection_info([dir_id])
        if info:
            return info[0]
        raise RoxyApiError(f"打开 Roxy Profile 失败：{data.get('msg') or data}")

    def connection_info(self, dir_ids: list[str] | None = None) -> list[dict]:
        params: dict[str, Any] = {}
        if dir_ids:
            params["dirIds"] = dir_ids
        data = self._request("GET", "/browser/connection_info", params=params)
        self._ensure_ok(data, "读取 Roxy 连接信息")
        payload = data.get("data")
        rows = payload.get("rows") if isinstance(payload, dict) else payload
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            return []
        if not dir_ids:
            return [row for row in rows if isinstance(row, dict)]
        targets = set(dir_ids)
        return [
            row
            for row in rows
            if isinstance(row, dict) and str(row.get("dirId") or row.get("id") or "") in targets
        ]

    def close_profile(self, dir_id: str) -> None:
        data = self._request("POST", "/browser/close", json_body={"dirId": dir_id})
        self._ensure_ok(data, "关闭 Roxy Profile")


def normalize_cdp_endpoint(value: str) -> str:
    endpoint = str(value or "").strip()
    if not endpoint:
        return ""
    if endpoint.startswith(("http://", "https://", "ws://", "wss://")):
        return endpoint
    return f"http://{endpoint}"


def extract_cdp_endpoint(connection_data: dict) -> str:
    candidates = [
        connection_data.get("http"),
        connection_data.get("ws"),
        connection_data.get("webSocketDebuggerUrl"),
        connection_data.get("debuggerAddress"),
    ]

    data = connection_data.get("data")
    if isinstance(data, dict):
        candidates.extend(
            [
                data.get("http"),
                data.get("ws"),
                data.get("webSocketDebuggerUrl"),
                data.get("debuggerAddress"),
            ]
        )

    for candidate in candidates:
        endpoint = normalize_cdp_endpoint(str(candidate or ""))
        if endpoint:
            return endpoint
    raise RoxyApiError(f"Roxy 没有返回 CDP 连接地址：{connection_data}")


def wait_for_cdp_endpoint(endpoint: str, timeout_seconds: int = 20) -> None:
    if endpoint.startswith(("ws://", "wss://")):
        return
    deadline = time.time() + timeout_seconds
    version_url = f"{endpoint.rstrip('/')}/json/version"
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = requests.get(version_url, timeout=1)
            if response.status_code == 200:
                return
        except Exception as exc:
            last_error = exc
        time.sleep(0.4)
    raise RoxyApiError(f"Roxy CDP 地址未就绪：{endpoint} ({last_error})")
