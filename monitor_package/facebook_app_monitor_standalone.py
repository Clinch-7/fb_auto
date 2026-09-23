from __future__ import annotations

import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_CONFIG: dict[str, Any] = {
    "lark": {
        "openapi_base": "https://open.larksuite.com",
        "app_id": "",
        "app_secret": "",
        "webhook_url": "",
        "at_members": [],
    },
    "sheet": {
        "spreadsheet_token": "",
        "sheet_id": "",
        "read_range": "A2:C1000",
        "update_range_template": "D{row}:G{row}",
    },
    "facebook": {
        "graph_version": "v25.0",
    },
    "runtime": {
        "check_interval_minutes": 30,
        "max_workers": 3,
        "request_timeout_seconds": [5, 20],
        "confirm_fail_times": 1,
        "confirm_recovery_times": 1,
        "force_alert_on_start": False,
    },
}


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = app_dir()
CONFIG_FILE = BASE_DIR / "app_monitor_config.json"
STATE_FILE = BASE_DIR / ".app_monitor_state.json"
LOG_FILE = BASE_DIR / "monitor.log"


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(base, ensure_ascii=False))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with CONFIG_FILE.open("w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        return DEFAULT_CONFIG

    with CONFIG_FILE.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"配置文件必须是 JSON object: {CONFIG_FILE}")
    return deep_merge(DEFAULT_CONFIG, data)


CONFIG = load_config()


def cfg(section: str, key: str, default: Any = "") -> Any:
    value = CONFIG.get(section, {})
    return value.get(key, default) if isinstance(value, dict) else default


def timeout_config() -> tuple[float, float]:
    raw = cfg("runtime", "request_timeout_seconds", [5, 20])
    if isinstance(raw, list) and len(raw) >= 2:
        return float(raw[0]), float(raw[1])
    return 5.0, 20.0


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("facebook_app_monitor")


session = requests.Session()
retry = Retry(
    total=3,
    connect=3,
    read=3,
    status=3,
    backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST", "PUT"],
)
adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
session.mount("https://", adapter)
session.mount("http://", adapter)

_lark_token_cache = {"token": "", "expire_at": 0.0}
_fb_token_cache: dict[str, dict[str, Any]] = {}


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_iso() -> str:
    return datetime.now().isoformat()


def openapi_base() -> str:
    base = str(cfg("lark", "openapi_base", "https://open.larksuite.com") or "https://open.larksuite.com").strip()
    if not base.startswith(("http://", "https://")):
        base = "https://open.larksuite.com"
    return base.rstrip("/")


def request_timeout() -> tuple[float, float]:
    return timeout_config()


def safe_json_response(resp: requests.Response) -> dict[str, Any] | None:
    try:
        return resp.json()
    except Exception:
        return None


def is_transient_fb_error(error_code: Any, error_message: str = "") -> bool:
    transient_codes = {"1", "2", "4", "17", "32", "613", "80004"}
    code = str(error_code) if error_code is not None else ""
    msg = str(error_message or "").lower()

    permanent_keywords = (
        "error validating client secret",
        "validating client secret",
        "invalid client secret",
        "client secret",
        "invalid app secret",
        "app secret",
        "invalid oauth access token",
        "invalid app id",
    )
    if any(keyword in msg for keyword in permanent_keywords):
        return False

    if code in transient_codes:
        return True
    return any(
        keyword in msg
        for keyword in (
            "temporarily unavailable",
            "please reduce the amount of data",
            "rate limit",
            "too many calls",
            "try again later",
            "service unavailable",
        )
    )


def get_lark_token() -> str:
    now = time.time()
    if _lark_token_cache["token"] and now < _lark_token_cache["expire_at"] - 60:
        return str(_lark_token_cache["token"])

    resp = session.post(
        f"{openapi_base()}/open-apis/auth/v3/tenant_access_token/internal",
        headers={"Content-Type": "application/json"},
        json={
            "app_id": str(cfg("lark", "app_id", "")).strip(),
            "app_secret": str(cfg("lark", "app_secret", "")).strip(),
        },
        timeout=request_timeout(),
    )
    data = safe_json_response(resp)
    if not data:
        logger.error("获取 Lark Token 失败，非 JSON 响应: status=%s text=%s", resp.status_code, resp.text[:300])
        return ""
    if data.get("code") != 0:
        logger.error("获取 Lark Token 失败: %s", data)
        return ""

    token = str(data.get("tenant_access_token") or "")
    _lark_token_cache["token"] = token
    _lark_token_cache["expire_at"] = now + int(data.get("expire", 7200) or 7200)
    return token


def read_facebook_apps_from_sheet() -> list[dict[str, Any]]:
    token = get_lark_token()
    if not token:
        return []

    spreadsheet_token = str(cfg("sheet", "spreadsheet_token", "")).strip()
    sheet_id = str(cfg("sheet", "sheet_id", "")).strip()
    read_range = str(cfg("sheet", "read_range", "A2:C1000")).strip()
    resp = session.get(
        f"{openapi_base()}/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{sheet_id}!{read_range}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=request_timeout(),
    )
    data = safe_json_response(resp)
    if not data:
        logger.error("读取表格失败，非 JSON 响应: status=%s text=%s", resp.status_code, resp.text[:300])
        return []
    if data.get("code") != 0:
        logger.error("读取表格失败: %s", data)
        return []

    apps: list[dict[str, Any]] = []
    rows = data.get("data", {}).get("valueRange", {}).get("values", [])
    for i, row in enumerate(rows if isinstance(rows, list) else [], start=2):
        if row and len(row) >= 3 and row[1] and row[2]:
            apps.append(
                {
                    "name": str(row[0]) if row[0] else f"APP_{row[1]}",
                    "app_id": str(row[1]).strip(),
                    "app_secret": str(row[2]).strip(),
                    "row": i,
                }
            )
    logger.info("从表格读取到 %s 个 APP 配置", len(apps))
    return apps


def update_sheet_row(row_num: int, online: bool | None, error_code: str, error_message: str) -> None:
    token = get_lark_token()
    if not token:
        return

    status_text = "在线" if online is True else "异常" if online is False else "网络异常"
    template = str(cfg("sheet", "update_range_template", "D{row}:G{row}") or "D{row}:G{row}")
    cell_range = template.format(row=row_num)
    payload = {
        "valueRange": {
            "range": f"{cfg('sheet', 'sheet_id')}!{cell_range}",
            "values": [[status_text, str(error_code) if error_code else "", error_message or "", now_str()]],
        }
    }
    resp = session.put(
        f"{openapi_base()}/open-apis/sheets/v2/spreadsheets/{cfg('sheet', 'spreadsheet_token')}/values",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=request_timeout(),
    )
    data = safe_json_response(resp)
    if not data:
        logger.error("更新表格失败，非 JSON 响应 row=%s status=%s text=%s", row_num, resp.status_code, resp.text[:300])
    elif data.get("code") != 0:
        logger.error("更新表格失败 row=%s: %s", row_num, data)


def get_facebook_app_token(app_id: str, app_secret: str) -> dict[str, Any]:
    now = time.time()
    cache_key = f"{app_id}:{app_secret}"
    cached = _fb_token_cache.get(cache_key)
    if cached and now < float(cached["expire_at"]) - 300:
        return {"ok": True, "token": cached["token"], "error_code": None, "error_message": None, "is_network_error": False}

    url = f"https://graph.facebook.com/{cfg('facebook', 'graph_version', 'v25.0')}/oauth/access_token"
    try:
        resp = session.get(
            url,
            params={"client_id": app_id, "client_secret": app_secret, "grant_type": "client_credentials"},
            timeout=request_timeout(),
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            return {
                "ok": False,
                "token": None,
                "error_code": str(resp.status_code),
                "error_message": f"Facebook 临时异常或限流: HTTP {resp.status_code}",
                "is_network_error": True,
            }
        data = safe_json_response(resp)
        if not data:
            return {
                "ok": False,
                "token": None,
                "error_code": "non_json_response",
                "error_message": f"Facebook Token 接口非 JSON 响应: HTTP {resp.status_code}",
                "is_network_error": True,
            }
        if "access_token" in data:
            token = str(data["access_token"])
            _fb_token_cache[cache_key] = {"token": token, "expire_at": now + 6 * 3600}
            return {"ok": True, "token": token, "error_code": None, "error_message": None, "is_network_error": False}
        if "error" in data:
            err = data["error"]
            code = err.get("code") or err.get("error_subcode") or "fb_token_error"
            message = err.get("message", "获取 App Token 失败")
            transient = is_transient_fb_error(code, message)
            logger.log(logging.WARNING if transient else logging.ERROR, "[%s] 获取 App Token 异常: code=%s msg=%s", app_id, code, message)
            return {"ok": False, "token": None, "error_code": str(code), "error_message": message, "is_network_error": transient}
        return {"ok": False, "token": None, "error_code": "unknown_token_response", "error_message": str(data), "is_network_error": True}
    except requests.exceptions.RequestException as exc:
        logger.error("[%s] 获取 App Token 网络异常: %s", app_id, exc)
        return {"ok": False, "token": None, "error_code": "request_exception", "error_message": str(exc), "is_network_error": True}
    except Exception as exc:
        logger.error("[%s] 获取 App Token 未知异常: %s", app_id, exc)
        return {"ok": False, "token": None, "error_code": "exception", "error_message": str(exc), "is_network_error": True}


def check_app_online(app_id: str, app_token: str) -> dict[str, Any]:
    url = f"https://graph.facebook.com/{cfg('facebook', 'graph_version', 'v25.0')}/{app_id}"
    try:
        resp = session.get(url, params={"fields": "id,name", "access_token": app_token}, timeout=request_timeout())
        if resp.status_code == 429 or resp.status_code >= 500:
            return {"online": None, "error_code": str(resp.status_code), "error_message": f"Facebook 临时异常或限流: HTTP {resp.status_code}"}
        data = safe_json_response(resp)
        if not data:
            return {"online": None, "error_code": "non_json_response", "error_message": f"Facebook App 查询接口非 JSON 响应: HTTP {resp.status_code}"}
        if "error" in data:
            err = data["error"]
            code = err.get("code") or err.get("error_subcode") or "fb_app_error"
            message = err.get("message", "")
            if is_transient_fb_error(code, message):
                return {"online": None, "error_code": str(code), "error_message": message}
            return {"online": False, "error_code": str(code), "error_message": message}
        if str(data.get("id")) == str(app_id):
            return {"online": True, "error_code": None, "error_message": None}
        return {"online": None, "error_code": "unexpected_response", "error_message": str(data)}
    except requests.exceptions.RequestException as exc:
        logger.error("[%s] 查询 App 网络异常: %s", app_id, exc)
        return {"online": None, "error_code": "request_exception", "error_message": str(exc)}
    except Exception as exc:
        logger.error("[%s] 查询 App 未知异常: %s", app_id, exc)
        return {"online": None, "error_code": "exception", "error_message": str(exc)}


def check_single_app(app_config: dict[str, Any]) -> dict[str, Any]:
    app_id = str(app_config["app_id"])
    token_result = get_facebook_app_token(app_id, str(app_config["app_secret"]))
    if not token_result["ok"]:
        return {
            "app_id": app_id,
            "name": app_config.get("name", app_id),
            "online": None if token_result["is_network_error"] else False,
            "error_code": token_result["error_code"],
            "error_message": token_result["error_message"],
            "row": app_config.get("row"),
        }
    result = check_app_online(app_id, str(token_result["token"]))
    result.update({"app_id": app_id, "name": app_config.get("name", app_id), "row": app_config.get("row")})
    return result


def _send_lark(payload: dict[str, Any]) -> None:
    webhook_url = str(cfg("lark", "webhook_url", "") or "").strip()
    if not webhook_url:
        return
    try:
        resp = session.post(webhook_url, json=payload, timeout=request_timeout())
        result = safe_json_response(resp)
        if result and result.get("code") == 0:
            logger.info("[飞书] 消息发送成功")
        else:
            logger.error("[飞书] 消息发送失败: status=%s result=%s text=%s", resp.status_code, result, resp.text[:300])
    except Exception as exc:
        logger.error("[飞书] 发送异常: %s", exc)


def send_lark_alert_batch(offline_apps: list[dict[str, Any]]) -> None:
    if not offline_apps:
        return
    content = [[{"tag": "text", "text": f"检测时间: {now_str()}\n\n"}]]
    for app in offline_apps:
        content.append(
            [
                {"tag": "text", "text": f"• {app['name']} [{app['app_id']}]\n"},
                {"tag": "text", "text": f"错误码: {app.get('error_code', '未知')} | {app.get('error_message', '未知错误')}\n\n"},
            ]
        )
    at_members = cfg("lark", "at_members", [])
    content.append(
        [
            *[{"tag": "at", "user_id": member["user_id"]} for member in at_members if isinstance(member, dict) and member.get("user_id")],
            {"tag": "text", "text": " 请立即处理！\n\n请登录 "},
            {"tag": "a", "text": "Facebook 开发者后台", "href": "https://developers.facebook.com/apps/"},
            {"tag": "text", "text": " 检查应用状态。"},
        ]
    )
    _send_lark(
        {
            "msg_type": "post",
            "content": {"post": {"zh_cn": {"title": f"Facebook 应用异常/下架（{len(offline_apps)}个）", "content": content}}},
        }
    )


def send_lark_recovery_batch(recovered_apps: list[dict[str, Any]]) -> None:
    if not recovered_apps:
        return
    content = [[{"tag": "text", "text": f"Facebook 应用已恢复（{len(recovered_apps)}个）\n恢复时间: {now_str()}\n\n"}]]
    for app in recovered_apps:
        content.append([{"tag": "text", "text": f"• {app['name']} [{app['app_id']}] 已恢复正常\n"}])
    at_members = cfg("lark", "at_members", [])
    content.append(
        [
            *[{"tag": "at", "user_id": member["user_id"]} for member in at_members if isinstance(member, dict) and member.get("user_id")],
            {"tag": "text", "text": " 以上应用已恢复正常，请确认。"},
        ]
    )
    _send_lark({"msg_type": "post", "content": {"post": {"zh_cn": {"title": "Facebook 应用已恢复", "content": content}}}})


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.error("读取状态文件失败: %s", exc)
    return {}


def save_state(state: dict[str, Any]) -> None:
    try:
        with STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.error("保存状态失败: %s", exc)


def build_next_state_and_notifications(results: list[dict[str, Any]], last_state: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    offline_apps: list[dict[str, Any]] = []
    recovered_apps: list[dict[str, Any]] = []
    current_state: dict[str, Any] = {}
    confirm_fail_times = int(cfg("runtime", "confirm_fail_times", 1) or 1)
    confirm_recovery_times = int(cfg("runtime", "confirm_recovery_times", 1) or 1)

    for result in results:
        app_id = str(result["app_id"])
        app_name = str(result.get("name") or app_id)
        is_online = result["online"]
        error_code = result.get("error_code")
        error_message = result.get("error_message")
        row = result.get("row")
        if row:
            update_sheet_row(int(row), is_online, str(error_code or ""), str(error_message or ""))

        old = last_state.get(app_id, {})
        old_confirmed_online = old.get("confirmed_online")
        old_fail_count = int(old.get("fail_count", 0) or 0)
        old_recovery_count = int(old.get("recovery_count", 0) or 0)

        if is_online is None:
            logger.warning("[%s] 本次为网络/临时异常，不参与状态判断: %s | %s", app_name, error_code, error_message)
            current_state[app_id] = {
                **old,
                "app_name": app_name,
                "last_check": now_iso(),
                "last_probe_online": None,
                "last_probe_status": error_code or "network_error",
                "last_network_error": error_message,
            }
            continue

        if is_online is True:
            recovery_count = old_recovery_count + 1 if old_confirmed_online is False else 0
            if old_confirmed_online is False and recovery_count >= confirm_recovery_times:
                recovered_apps.append(result)
                confirmed_online = True
                recovery_count = 0
            else:
                confirmed_online = True if old_confirmed_online is not False else False
            current_state[app_id] = {
                "confirmed_online": confirmed_online,
                "fail_count": 0,
                "recovery_count": recovery_count,
                "last_probe_online": True,
                "last_probe_status": "active",
                "app_name": app_name,
                "last_check": now_iso(),
            }
            continue

        fail_count = old_fail_count + 1
        confirmed_online = old_confirmed_online
        if old_confirmed_online is not False and fail_count >= confirm_fail_times:
            offline_apps.append(result)
            confirmed_online = False
        current_state[app_id] = {
            "confirmed_online": confirmed_online,
            "fail_count": fail_count,
            "recovery_count": 0,
            "last_probe_online": False,
            "last_probe_status": error_code or "app_error",
            "last_error_message": error_message,
            "app_name": app_name,
            "last_check": now_iso(),
        }

    return current_state, offline_apps, recovered_apps


def run_once(last_state: dict[str, Any]) -> dict[str, Any]:
    apps = read_facebook_apps_from_sheet()
    if not apps:
        logger.warning("表格中没有配置任何 APP")
        return last_state

    results: list[dict[str, Any]] = []
    max_workers = int(cfg("runtime", "max_workers", 3) or 3)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(check_single_app, app): app for app in apps}
        for future in as_completed(futures):
            app = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                logger.error("[%s] 检查异常: %s", app.get("name") or app.get("app_id"), exc)
                results.append(
                    {
                        "app_id": app.get("app_id"),
                        "name": app.get("name") or app.get("app_id"),
                        "online": None,
                        "error_code": "future_exception",
                        "error_message": str(exc),
                        "row": app.get("row"),
                    }
                )

    current_state, offline_apps, recovered_apps = build_next_state_and_notifications(results, last_state)
    if offline_apps:
        send_lark_alert_batch(offline_apps)
    if recovered_apps:
        send_lark_recovery_batch(recovered_apps)

    online_count = sum(1 for item in results if item["online"] is True)
    offline_count = sum(1 for item in results if item["online"] is False)
    network_error_count = sum(1 for item in results if item["online"] is None)
    logger.info(
        "汇总: 在线 %s | 真实异常 %s | 网络/临时异常 %s | 新告警 %s | 恢复 %s",
        online_count,
        offline_count,
        network_error_count,
        len(offline_apps),
        len(recovered_apps),
    )
    return current_state


def main() -> None:
    interval_minutes = float(cfg("runtime", "check_interval_minutes", 30) or 30)
    check_interval = max(60, int(interval_minutes * 60))
    logger.info("=" * 60)
    logger.info("Facebook App 下架监测启动")
    logger.info("配置文件: %s", CONFIG_FILE)
    logger.info("Graph API: %s", cfg("facebook", "graph_version", "v25.0"))
    logger.info("检查间隔: %s 秒", check_interval)
    logger.info("连续异常确认次数: %s", cfg("runtime", "confirm_fail_times", 1))
    logger.info("=" * 60)

    if bool(cfg("runtime", "force_alert_on_start", False)) and STATE_FILE.exists():
        STATE_FILE.unlink()
    last_state = load_state()

    while True:
        try:
            last_state = run_once(last_state)
            save_state(last_state)
        except KeyboardInterrupt:
            logger.info("脚本退出")
            break
        except Exception as exc:
            logger.exception("主循环异常: %s", exc)
        time.sleep(check_interval)


if __name__ == "__main__":
    main()
