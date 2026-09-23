from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import pyotp
import requests

from app_config import required, value


DEFAULT_OPS_LOGIN_URL = "https://ga-client.ops-center.net/api/user/login/"
DEFAULT_VEST_LOGIN_URL = "https://api-vest.tgcgame.com/admin/login_by_token/"
DEFAULT_VEST_API_BASE = "https://api-vest.tgcgame2026.com"
DEFAULT_ORGANIZATION = "Quest"


def _vest_value(key: str, default: str = "") -> str:
    return str(value("vest", key, default) or default).strip()


@dataclass
class VestAppInfo:
    app_id: str
    brand_name: str
    package_name: str
    fb_key_hash: str
    deploy_key_hash: str
    remarks_url: str
    raw: dict[str, Any]


class VestService:
    def __init__(self) -> None:
        self.token = ""
        self.user_id = ""
        self.ops_login_url = _vest_value("ops_login_url", DEFAULT_OPS_LOGIN_URL)
        self.vest_login_url = _vest_value("vest_login_url", DEFAULT_VEST_LOGIN_URL)
        self.vest_api_base = _vest_value("vest_api_base", DEFAULT_VEST_API_BASE).rstrip("/")
        self.organization = _vest_value("organization", DEFAULT_ORGANIZATION)
        self.ops_username = required("vest", "ops_username", "vest.ops_username")
        self.ops_password = required("vest", "ops_password", "vest.ops_password")
        self.ops_totp_secret = required("vest", "ops_totp_secret", "vest.ops_totp_secret")

    def login(self, max_retries: int = 3) -> None:
        totp = pyotp.TOTP(self.ops_totp_secret)
        last_error: Exception | None = None

        for attempt in range(1, max_retries + 1):
            try:
                code = totp.now()
                resp = requests.post(
                    self.ops_login_url,
                    json={"username": self.ops_username, "password": self.ops_password},
                    headers={"Content-Type": "application/json;charset=UTF-8", "X-Auth-Vcode": code},
                    timeout=15,
                )
                data = resp.json()
                callback_token = data.get("data", {}).get("callback_token")
                if not callback_token:
                    raise RuntimeError(data.get("msg") or data.get("message") or f"未获取 callback_token: {data}")

                resp2 = requests.post(
                    self.vest_login_url,
                    json={"callback_token": callback_token},
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "https://vest-v2.tgcgame2026.com",
                        "Referer": "https://vest-v2.tgcgame2026.com/",
                    },
                    timeout=15,
                )
                data2 = resp2.json()
                token = data2.get("data", {}).get("token")
                user_id = data2.get("data", {}).get("id")
                if not token or user_id is None:
                    raise RuntimeError(data2.get("msg") or data2.get("message") or f"Vest token 获取失败: {data2}")

                self.token = token
                self.user_id = str(user_id)
                return
            except Exception as exc:
                last_error = exc
                if attempt < max_retries:
                    wait_seconds = max(2, int(31 - (time.time() % 30)))
                    time.sleep(wait_seconds)

        raise RuntimeError(f"平台中心登录失败，已重试 {max_retries} 次: {last_error}")

    def headers(self) -> dict[str, str]:
        if not self.token or not self.user_id:
            raise RuntimeError("VestService 尚未登录")
        return {
            "Origin": "https://vest-v2.tgcgame2026.com",
            "Referer": "https://vest-v2.tgcgame2026.com/",
            "x-auth-token": self.token,
            "x-auth-user": self.user_id,
        }

    def get_brand_app(self, brand_name: str) -> VestAppInfo:
        params = {
            "organization": self.organization,
            "brand_name": brand_name,
            "time_type": 1,
            "page_no": 1,
            "page_size": 20,
        }
        resp = requests.get(f"{self.vest_api_base}/list/app/v2", params=params, headers=self.headers(), timeout=20)
        data = resp.json()
        items = data.get("data", {}).get("items", [])
        if not items:
            raise RuntimeError(f"未找到品牌 {brand_name}: {data}")

        app_id = str(items[0]["id"])
        detail = requests.get(f"{self.vest_api_base}/list/app/v2/{app_id}/", headers=self.headers(), timeout=20).json()["data"]
        return VestAppInfo(
            app_id=app_id,
            brand_name=brand_name,
            package_name=detail["product_info"]["package_name"],
            fb_key_hash=detail["extension_info"]["fb_app_key"],
            deploy_key_hash=detail["extension_info"]["deploy_key_hash"],
            remarks_url=detail["review_info"]["remark"],
            raw=detail,
        )

    def update_fb_app_id(self, app: VestAppInfo, fb_app_id: str) -> None:
        d = app.raw
        patch_data = {
            "status_info": {
                "app_type": d["status_info"]["app_type"],
                "app_status": d["status_info"]["app_status"],
                "app_kind": d["status_info"]["app_kind"],
                "approval_time": d["status_info"]["approval_time"],
                "product_username": d["status_info"]["product_username"],
                "organization": d["status_info"]["organization"],
                "prefix_kind": d["status_info"]["prefix_kind"],
            },
            "review_info": {
                "referral_time": d["review_info"]["referral_time"],
                "remark": d["review_info"]["remark"],
                "virtual_addr": d["review_info"]["virtual_addr"],
            },
            "product_info": {
                "app_name": d["product_info"]["app_name"],
                "app_category": d["product_info"]["app_category"],
                "package_name": d["product_info"]["package_name"],
                "platform": d["product_info"]["platform"],
                "release_countries": d["product_info"]["release_countries"],
            },
            "extension_info": {
                "app_store_url": d["extension_info"]["app_store_url"],
                "fb_app_key": d["extension_info"]["fb_app_key"],
                "fb_app_code": fb_app_id,
                "deploy_key_hash": d["extension_info"]["deploy_key_hash"],
            },
            "client_info": {},
        }
        resp = requests.patch(
            f"{self.vest_api_base}/list/app/v2/{app.app_id}/",
            json=patch_data,
            headers={**self.headers(), "accept": "application/json, text/plain, */*", "content-type": "application/json"},
            timeout=20,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Vest 后台更新失败: status={resp.status_code}, text={resp.text[:300]}")
