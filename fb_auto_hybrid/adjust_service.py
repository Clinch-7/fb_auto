from __future__ import annotations

import copy
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

from app_config import AppConfigError, section


ADJUST_DASHBOARD_API = "https://api.adjust.com/dashboard/api"
ADJUST_AUTOMATION_API = "https://automate.adjust.com/app-automation"
FACEBOOK_PARTNER_ID = 34
TIKTOK_PARTNER_ID = 2337

DEFAULT_FACEBOOK_EVENT_MAPPINGS = {
    "Register": "fb_mobile_complete_registration",
    "Small": "fb_mobile_purchase",
    "Medium": "fb_mobile_purchase",
    "Large": "fb_mobile_purchase",
}

DEFAULT_TIKTOK_EVENT_MAPPINGS = {
    "Register": "register",
    "Small": "pay",
    "Medium": "pay",
    "Large": "pay",
}


@dataclass
class AdjustApp:
    token: str
    name: str
    package_name: str
    has_partner_integration: bool
    raw: dict[str, Any]


@dataclass
class AdjustFacebookPartnerResult:
    app_token: str
    app_name: str
    mode: str
    fb_app_id: str
    created_events: list[str]
    mapped_events: dict[str, str]
    status: int
    ok: bool


@dataclass
class AdjustTikTokPartnerResult:
    app_token: str
    app_name: str
    tiktok_app_id: str
    added_ids: list[str]
    duplicate_ids: list[str]
    submitted_ids: list[str]
    mapped_events: dict[str, str]
    status: int
    ok: bool


class AdjustApiError(RuntimeError):
    pass


class AdjustService:
    def __init__(
        self,
        api_token: str,
        *,
        base_url: str = ADJUST_DASHBOARD_API,
        automation_base_url: str = ADJUST_AUTOMATION_API,
    ) -> None:
        self.api_token = api_token.strip()
        if not self.api_token:
            raise AppConfigError("Please configure adjust.api_token in app_config.json")
        self.base_url = base_url.rstrip("/")
        self.automation_base_url = automation_base_url.rstrip("/")
        self.session = requests.Session()

    @classmethod
    def from_config(cls) -> "AdjustService":
        cfg = section("adjust")
        return cls(
            str(cfg.get("api_token", "") or ""),
            base_url=str(cfg.get("api_base", ADJUST_DASHBOARD_API)),
            automation_base_url=str(cfg.get("automation_api_base", ADJUST_AUTOMATION_API)),
        )

    @staticmethod
    def enabled() -> bool:
        cfg = section("adjust")
        return bool(cfg.get("enabled", True))

    @staticmethod
    def event_mappings_from_config() -> dict[str, str]:
        cfg = section("adjust")
        configured = cfg.get("facebook_event_mappings")
        if isinstance(configured, dict) and configured:
            return {str(k): str(v) for k, v in configured.items() if str(k).strip() and str(v).strip()}
        return dict(DEFAULT_FACEBOOK_EVENT_MAPPINGS)

    @staticmethod
    def tiktok_event_mappings_from_config() -> dict[str, str]:
        cfg = section("adjust")
        configured = cfg.get("tiktok_event_mappings")
        if isinstance(configured, dict) and configured:
            return {str(k): str(v) for k, v in configured.items() if str(k).strip() and str(v).strip()}
        return dict(DEFAULT_TIKTOK_EVENT_MAPPINGS)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-source": "dashboard-ptl",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        headers = kwargs.pop("headers", {})
        merged_headers = {**self._headers(), **headers}
        resp = self.session.request(method, url, headers=merged_headers, timeout=30, **kwargs)
        if resp.status_code >= 400:
            raise AdjustApiError(f"Adjust {method} {url} failed: HTTP {resp.status_code} | {resp.text[:800]}")
        content_type = resp.headers.get("content-type", "")
        if "json" in content_type:
            try:
                data = resp.json()
            except Exception:
                data = None
            if isinstance(data, dict) and (data.get("error") or data.get("errors") or data.get("error_code")):
                raise AdjustApiError(f"Adjust {method} {url} returned error: {str(data)[:800]}")
        return resp

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        resp = self._request(method, path, **kwargs)
        try:
            data = resp.json()
        except Exception as exc:
            raise AdjustApiError(f"Adjust {method} {path} returned non-json response: {resp.text[:500]}") from exc
        if not isinstance(data, dict):
            raise AdjustApiError(f"Adjust {method} {path} returned unexpected json: {type(data).__name__}")
        return data

    def _automation_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{self.automation_base_url}/{path.lstrip('/')}"
        headers = kwargs.pop("headers", {})
        merged_headers = {**self._headers(), **headers}
        resp = self.session.request(method, url, headers=merged_headers, timeout=45, **kwargs)
        if resp.status_code >= 400:
            if resp.status_code == 403 and "app automation" in (resp.text or "").lower():
                raise AdjustApiError(
                    "Adjust 未开通 App Automation，无法自动创建 Adjust App；"
                    "请先在 Adjust 手动创建该品牌 App，或联系 Adjust 开通 App Automation 权限。"
                )
            raise AdjustApiError(f"Adjust automation {method} {url} failed: HTTP {resp.status_code} | {resp.text[:800]}")
        try:
            data = resp.json()
        except Exception as exc:
            raise AdjustApiError(f"Adjust automation {method} {url} returned non-json response: {resp.text[:500]}") from exc
        if not isinstance(data, dict):
            raise AdjustApiError(f"Adjust automation {method} {url} returned unexpected json: {type(data).__name__}")
        return data

    def search_apps(self, brand: str, *, connected: bool | None) -> list[AdjustApp]:
        adjust_brand = str(brand).strip()
        params: dict[str, Any] = {
            "can_create_tracker": "true",
            "page[total]": "false",
            "page[per]": "50",
            "search": adjust_brand,
            "platform_presence": "true",
            "connected_partner_ids": str(FACEBOOK_PARTNER_ID),
            "has_integration_for_partner_id": str(FACEBOOK_PARTNER_ID),
            "web_only": "false",
        }
        if connected is not None:
            params["connected"] = "true" if connected else "false"

        data = self._json("GET", "apps", params=params)
        return self._parse_apps_response(data)

    def search_apps_plain(self, brand: str) -> list[AdjustApp]:
        adjust_brand = str(brand).strip()
        params: dict[str, Any] = {
            "can_create_tracker": "true",
            "page[total]": "false",
            "page[per]": "50",
            "search": adjust_brand,
            "platform_presence": "true",
            "web_only": "false",
        }
        data = self._json("GET", "apps", params=params)
        return self._parse_apps_response(data)

    def search_partner_mobile_apps(self, brand: str, partner_id: int = FACEBOOK_PARTNER_ID) -> list[AdjustApp]:
        adjust_brand = str(brand).strip()
        params: dict[str, Any] = {
            "can_create_tracker": "true",
            "search": adjust_brand,
            "web_only": "false",
        }
        data = self._json("GET", f"ptl/partners/networks/{partner_id}/mobile_apps", params=params)
        return self._parse_apps_response(data)

    def _parse_apps_response(self, data: dict[str, Any]) -> list[AdjustApp]:
        rows = data.get("apps") or data.get("data") or data.get("mobile_apps") or []
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("apps") or rows.get("mobile_apps") or rows.get("items") or []
        if not isinstance(rows, list):
            raise AdjustApiError(f"Adjust app search returned unexpected rows: {type(rows).__name__}")
        return [self._parse_app(row) for row in rows if isinstance(row, dict)]

    def resolve_brand_app(
        self,
        brand: str,
        *,
        package_name: str | None = None,
        app_display_name: str | None = None,
    ) -> tuple[AdjustApp, str]:
        adjust_brand = str(brand).strip()
        connected = self.search_apps(brand, connected=True)
        if connected:
            return self._pick_brand_match(adjust_brand, connected), "edit"

        partner_apps = self.search_partner_mobile_apps(brand)
        if partner_apps:
            app = self._pick_brand_match(adjust_brand, partner_apps)
            if app.has_partner_integration or self.has_saved_facebook_partner_settings(app.token):
                return app, "edit"

        unconnected = self.search_apps(brand, connected=False)
        if unconnected:
            return self._pick_brand_match(adjust_brand, unconnected), "create"

        any_match = self.search_apps(brand, connected=None)
        if any_match:
            app = self._pick_brand_match(adjust_brand, any_match)
            return app, "edit" if self.has_saved_facebook_partner_settings(app.token) else "create"

        if package_name:
            app = self.create_adjust_app(
                brand=adjust_brand,
                package_name=package_name,
                app_display_name=app_display_name,
            )
            return app, "create_adjust_app"

        raise AdjustApiError(f"No Adjust app found for brand {adjust_brand}")

    def ensure_facebook_partner(
        self,
        brand: str,
        fb_app_id: str,
        *,
        event_mappings: dict[str, str] | None = None,
        package_name: str | None = None,
        app_display_name: str | None = None,
    ) -> AdjustFacebookPartnerResult:
        mappings = event_mappings or self.event_mappings_from_config()
        app, mode = self.resolve_brand_app(
            brand,
            package_name=package_name,
            app_display_name=app_display_name,
        )
        created_events = self.ensure_events(app.token, mappings.keys())
        partner_settings = self.wait_for_partner_events(app.token, mappings.keys())

        if mode in ("create", "create_adjust_app"):
            try:
                payload = self.build_facebook_partner_link_payload(app.token, partner_settings, fb_app_id, mappings)
                resp = self.link_app_to_partner(payload)
                return AdjustFacebookPartnerResult(
                    app_token=app.token,
                    app_name=app.name,
                    mode=mode,
                    fb_app_id=fb_app_id,
                    created_events=created_events,
                    mapped_events=mappings,
                    status=resp.status_code,
                    ok=resp.ok,
                )
            except AdjustApiError as exc:
                fallback_app = self.find_connected_brand_app(brand)
                if not fallback_app:
                    raise
                app = fallback_app
                mode = "edit"
                created_events = self.ensure_events(app.token, mappings.keys())
            partner_settings = self.wait_for_partner_events(app.token, mappings.keys())

        payload = self.build_facebook_partner_payload(partner_settings, fb_app_id, mappings)
        resp = self._request("POST", f"apps/{app.token}/settings/partners", json=payload)
        return AdjustFacebookPartnerResult(
            app_token=app.token,
            app_name=app.name,
            mode=mode,
            fb_app_id=fb_app_id,
            created_events=created_events,
            mapped_events=mappings,
            status=resp.status_code,
            ok=resp.ok,
        )

    def ensure_tiktok_partner_app_id(
        self,
        brand: str,
        tiktok_app_id: str,
        *,
        event_mappings: dict[str, str] | None = None,
    ) -> AdjustTikTokPartnerResult:
        clean_brand = str(brand).strip()
        clean_app_id = str(tiktok_app_id).strip()
        if not clean_brand:
            raise AdjustApiError("Adjust brand is empty; cannot update TikTok App ID")
        if not clean_app_id:
            raise AdjustApiError(f"TikTok App ID is empty for brand {clean_brand}")

        apps = self._unique_apps(
            [
                *self.search_partner_mobile_apps(clean_brand, partner_id=TIKTOK_PARTNER_ID),
                *self.search_apps_plain(clean_brand),
            ]
        )
        if not apps:
            raise AdjustApiError(f"No Adjust app found for brand {clean_brand}")

        app = self._pick_brand_match(clean_brand, apps)
        settings = self.get_partner_settings(app.token, partner_id=TIKTOK_PARTNER_ID)
        mappings = event_mappings or self.tiktok_event_mappings_from_config()
        payload, id_stats = self.build_tiktok_partner_payload(settings, clean_app_id, mappings)
        resp = self._request("POST", f"apps/{app.token}/settings/partners", json=payload)
        return AdjustTikTokPartnerResult(
            app_token=app.token,
            app_name=app.name,
            tiktok_app_id=clean_app_id,
            added_ids=id_stats["added_ids"],
            duplicate_ids=id_stats["duplicate_ids"],
            submitted_ids=id_stats["submitted_ids"],
            mapped_events=mappings,
            status=resp.status_code,
            ok=resp.ok,
        )

    def link_app_to_partner(self, payload: dict[str, Any]) -> requests.Response:
        return self._request("POST", f"ptl/partners/networks/{FACEBOOK_PARTNER_ID}/link", json=payload)

    def create_adjust_app(self, *, brand: str, package_name: str, app_display_name: str | None = None) -> AdjustApp:
        clean_brand = str(brand).strip()
        clean_package = str(package_name).strip()
        if not clean_brand:
            raise AdjustApiError("Adjust app brand is empty; cannot create app")
        if not clean_package:
            raise AdjustApiError(f"Adjust app package is empty for brand {clean_brand}; cannot create app")
        display_name = str(app_display_name or clean_brand).strip() or clean_brand
        payload = {
            "name": display_name,
            "platform": "android",
            "store_id": clean_package,
            "bundle_id": clean_package,
        }
        data = self._automation_json("POST", "app", json=payload)
        token = str(
            data.get("adjust_app_token")
            or data.get("app_token")
            or data.get("token")
            or data.get("data", {}).get("adjust_app_token")
            or data.get("data", {}).get("app_token")
            or data.get("data", {}).get("token")
            or ""
        ).strip()
        if not token:
            raise AdjustApiError(f"Adjust app create did not return app token: {str(data)[:800]}")
        return AdjustApp(
            token=token,
            name=display_name,
            package_name=clean_package,
            has_partner_integration=False,
            raw=data,
        )

    def find_connected_brand_app(self, brand: str) -> AdjustApp | None:
        adjust_brand = str(brand).strip()
        try:
            connected = self._unique_apps(
                [
                    *self.search_partner_mobile_apps(brand),
                    *self.search_apps(brand, connected=True),
                    *self.search_apps(brand, connected=None),
                ]
            )
        except AdjustApiError:
            return None
        if not connected:
            return None
        app = self._pick_brand_match(adjust_brand, connected)
        if not app.has_partner_integration and not self.has_saved_facebook_partner_settings(app.token):
            return None
        return app

    def get_partner_settings(self, app_token: str, partner_id: int = FACEBOOK_PARTNER_ID) -> dict[str, Any]:
        return self._json("GET", f"apps/{app_token}/settings/partners/{partner_id}")

    def has_saved_facebook_partner_settings(self, app_token: str) -> bool:
        try:
            settings = self.get_partner_settings(app_token)
        except AdjustApiError:
            return False
        return self.partner_settings_has_facebook_app(settings)

    @staticmethod
    def partner_settings_has_facebook_app(settings: dict[str, Any]) -> bool:
        if settings.get("partner_integrated") is True:
            return True
        android = (settings.get("partner_app_settings") or {}).get("android") or {}
        if not isinstance(android, dict):
            return False
        if android.get("integrated") is True:
            return True
        for item in android.get("settings") or []:
            if not isinstance(item, dict):
                continue
            if item.get("key") == "app_id" and str(item.get("value") or "").strip():
                return True
        return False

    @staticmethod
    def _unique_apps(apps: list[AdjustApp]) -> list[AdjustApp]:
        seen: set[str] = set()
        unique: list[AdjustApp] = []
        for app in apps:
            if app.token in seen:
                continue
            seen.add(app.token)
            unique.append(app)
        return unique

    def wait_for_partner_events(self, app_token: str, event_names: Any, *, attempts: int = 6) -> dict[str, Any]:
        wanted = {str(name).strip() for name in event_names if str(name).strip()}
        last_settings: dict[str, Any] = {}
        for attempt in range(attempts):
            last_settings = self.get_partner_settings(app_token)
            existing = {
                str(item.get("event_type_name") or "")
                for item in last_settings.get("partner_event_type_settings") or []
                if isinstance(item, dict)
            }
            if wanted.issubset(existing):
                return last_settings
            if attempt < attempts - 1:
                time.sleep(1.5)
        return last_settings

    def list_events(self, app_token: str) -> list[dict[str, Any]]:
        data = self._json("GET", f"apps/{app_token}/event_types", params={"include_archived": "true", "page": 1, "per": 100})
        events = data.get("events") or []
        if not isinstance(events, list):
            raise AdjustApiError(f"Adjust events response is invalid for {app_token}")
        return [event for event in events if isinstance(event, dict)]

    def ensure_events(self, app_token: str, event_names: Any) -> list[str]:
        required = [str(name).strip() for name in event_names if str(name).strip()]
        last_error: AdjustApiError | None = None
        for attempt in range(6):
            try:
                existing_events = self.list_events(app_token)
                break
            except AdjustApiError as exc:
                last_error = exc
                if attempt < 5:
                    time.sleep(2)
                    continue
                raise
        existing_names = {str(event.get("name") or "") for event in existing_events}
        created: list[str] = []
        for name in required:
            if name in existing_names:
                continue
            self._request("POST", f"apps/{app_token}/event_types", json={"name": name, "unique": "false"})
            created.append(name)
        return created

    def build_facebook_partner_payload(
        self,
        partner_settings: dict[str, Any],
        fb_app_id: str,
        event_mappings: dict[str, str],
    ) -> dict[str, Any]:
        settings_by_platform = partner_settings.get("partner_app_settings")
        if not isinstance(settings_by_platform, dict) or "android" not in settings_by_platform:
            raise AdjustApiError("Adjust Facebook partner settings do not include android platform")

        partner_app_settings: dict[str, Any] = {}
        for platform, platform_settings in settings_by_platform.items():
            if not isinstance(platform_settings, dict):
                continue
            cleaned = copy.deepcopy(platform_settings)
            raw_settings = cleaned.pop("settings", [])
            cleaned["parameter_mapping"] = cleaned.get("parameter_mapping") or {}
            cleaned["parameter_sharing"] = cleaned.get("parameter_sharing") or []
            cleaned["ad_revenue_forwarding_sources"] = cleaned.get("ad_revenue_forwarding_sources") or []
            if platform == "android":
                cleaned["integrated"] = True
                cleaned["app_id"] = str(fb_app_id)
                for item in raw_settings:
                    if isinstance(item, dict) and item.get("key") and item.get("key") != "app_id":
                        value = item.get("value")
                        if value not in (None, ""):
                            cleaned[str(item["key"])] = value
            partner_app_settings[platform] = cleaned

        partner_event_type_settings = []
        wanted = {name: target for name, target in event_mappings.items()}
        for item in partner_settings.get("partner_event_type_settings") or []:
            if not isinstance(item, dict):
                continue
            event_name = str(item.get("event_type_name") or "")
            if event_name not in wanted:
                continue
            event_id = item.get("event_type_id")
            if event_id is None:
                continue
            partner_event_type_settings.append(
                {
                    "event_type_id": event_id,
                    "android": {"name": wanted[event_name]},
                }
            )

        found_names = {
            str(item.get("event_type_name") or "")
            for item in partner_settings.get("partner_event_type_settings") or []
            if isinstance(item, dict)
        }
        missing = [name for name in wanted if name not in found_names]
        if missing:
            raise AdjustApiError(f"Adjust events missing from partner settings after creation: {', '.join(missing)}")

        return {
            "id": FACEBOOK_PARTNER_ID,
            "partner_app_settings": partner_app_settings,
            "partner_event_type_settings": partner_event_type_settings,
        }

    def build_facebook_partner_link_payload(
        self,
        app_token: str,
        partner_settings: dict[str, Any],
        fb_app_id: str,
        event_mappings: dict[str, str],
    ) -> dict[str, Any]:
        base_payload = self.build_facebook_partner_payload(partner_settings, fb_app_id, event_mappings)
        android_settings = (base_payload.get("partner_app_settings") or {}).get("android") or {}
        if not isinstance(android_settings, dict):
            raise AdjustApiError("Adjust Facebook partner link payload is missing android settings")

        link_android_settings = {
            "app_id": str(fb_app_id),
            "integrated": False,
            "revenue_forwarding": bool(android_settings.get("revenue_forwarding", True)),
            "parameter_forwarding": bool(android_settings.get("parameter_forwarding", False)),
            "session_forwarding": bool(android_settings.get("session_forwarding", False)),
            "cost_forwarding": bool(android_settings.get("cost_forwarding", False)),
            "attributed_only": bool(android_settings.get("attributed_only", False)),
            "uninstall_forwarding": bool(android_settings.get("uninstall_forwarding", False)),
            "ad_revenue_forwarding": bool(android_settings.get("ad_revenue_forwarding", False)),
            "ad_revenue_forwarding_sources": android_settings.get("ad_revenue_forwarding_sources") or [],
            "skadnetwork_forwarding": bool(android_settings.get("skadnetwork_forwarding", False)),
            "limited_data_use": bool(android_settings.get("limited_data_use", True)),
            "inactive_user_definition": bool(android_settings.get("inactive_user_definition", True)),
            "attribution_window": bool(android_settings.get("attribution_window", False)),
            "parameter_sharing": android_settings.get("parameter_sharing") or [],
            "parameter_mapping": android_settings.get("parameter_mapping") or {},
        }

        return {
            "mobile_apps": [
                {
                    "app_token": app_token,
                    "partner_app_settings": {"android": link_android_settings},
                    "partner_event_type_settings": base_payload["partner_event_type_settings"],
                }
            ]
        }

    def build_tiktok_partner_payload(
        self,
        partner_settings: dict[str, Any],
        tiktok_app_id: str,
        event_mappings: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, list[str]]]:
        settings_by_platform = partner_settings.get("partner_app_settings")
        android_settings: dict[str, Any] = {}
        if isinstance(settings_by_platform, dict) and isinstance(settings_by_platform.get("android"), dict):
            android_settings = copy.deepcopy(settings_by_platform["android"])

        raw_settings = android_settings.pop("settings", [])
        existing_app_id = str(android_settings.get("app_id") or "").strip()
        for item in raw_settings:
            if isinstance(item, dict) and item.get("key") == "app_id":
                existing_app_id = str(item.get("value") or existing_app_id).strip()
                break
        merged_app_id, id_stats = self._merge_partner_app_ids(existing_app_id, str(tiktok_app_id))
        android_settings.update(
            {
                "enabled": True,
                "app_id": merged_app_id,
                "integrated": True,
                "revenue_forwarding": bool(android_settings.get("revenue_forwarding", True)),
                "parameter_forwarding": bool(android_settings.get("parameter_forwarding", True)),
                "session_forwarding": bool(android_settings.get("session_forwarding", True)),
                "cost_forwarding": bool(android_settings.get("cost_forwarding", False)),
                "attributed_only": bool(android_settings.get("attributed_only", False)),
                "uninstall_forwarding": bool(android_settings.get("uninstall_forwarding", False)),
                "ad_revenue_forwarding": bool(android_settings.get("ad_revenue_forwarding", False)),
                "ad_revenue_forwarding_sources": android_settings.get("ad_revenue_forwarding_sources") or [],
                "skadnetwork_forwarding": bool(android_settings.get("skadnetwork_forwarding", False)),
                "limited_data_use": bool(android_settings.get("limited_data_use", False)),
                "inactive_user_definition": bool(android_settings.get("inactive_user_definition", True)),
                "attribution_window": bool(android_settings.get("attribution_window", False)),
                "parameter_sharing": android_settings.get("parameter_sharing") or [],
                "parameter_mapping": android_settings.get("parameter_mapping") or {},
            }
        )
        for item in raw_settings:
            if isinstance(item, dict) and item.get("key") and item.get("key") != "app_id":
                value = item.get("value")
                if value not in (None, ""):
                    android_settings[str(item["key"])] = value

        partner_event_type_settings: list[dict[str, Any]] = []
        mapped_events: set[str] = set()
        for item in partner_settings.get("partner_event_type_settings") or []:
            if not isinstance(item, dict):
                continue
            event_id = item.get("event_type_id")
            if event_id is None:
                continue
            event_name = str(item.get("event_type_name") or "")
            partner_name = event_mappings.get(event_name)
            partner_event_type_settings.append({"event_type_id": event_id, "android": {"name": partner_name}})
            if event_name:
                mapped_events.add(event_name)

        if not partner_event_type_settings:
            raise AdjustApiError("Adjust TikTok partner settings returned no events to map")

        return (
            {
                "id": TIKTOK_PARTNER_ID,
                "partner_app_settings": {"android": android_settings},
                "partner_event_type_settings": partner_event_type_settings,
            },
            id_stats,
        )

    @staticmethod
    def _merge_partner_app_ids(existing: str, incoming: str) -> tuple[str, dict[str, list[str]]]:
        merged: list[str] = []
        seen: set[str] = set()
        existing_ids: list[str] = []
        for value in re.split(r"[\s,，、]+", str(existing or "")):
            clean_value = value.strip()
            if not clean_value or clean_value in seen:
                continue
            seen.add(clean_value)
            existing_ids.append(clean_value)
            merged.append(clean_value)

        added_ids: list[str] = []
        duplicate_ids: list[str] = []
        incoming_seen: set[str] = set()
        existing_set = set(existing_ids)
        for value in re.split(r"[\s,，、]+", str(incoming or "")):
            clean_value = value.strip()
            if not clean_value or clean_value in incoming_seen:
                continue
            incoming_seen.add(clean_value)
            if clean_value in existing_set:
                duplicate_ids.append(clean_value)
                continue
            added_ids.append(clean_value)
            existing_set.add(clean_value)
            merged.append(clean_value)

        return ",".join(merged), {
            "added_ids": added_ids,
            "duplicate_ids": duplicate_ids,
            "submitted_ids": merged,
        }

    @staticmethod
    def _partner_event_android_name(item: dict[str, Any]) -> str:
        android = item.get("android")
        if isinstance(android, dict):
            return str(android.get("name") or "").strip()
        if isinstance(android, list):
            for setting in android:
                if isinstance(setting, dict) and setting.get("key") == "name":
                    return str(setting.get("value") or "").strip()
        return ""

    @staticmethod
    def _parse_app(row: dict[str, Any]) -> AdjustApp:
        token = str(row.get("token") or row.get("app_token") or row.get("id") or "").strip()
        if not token:
            raise AdjustApiError(f"Adjust app row has no token: {row}")
        partner_settings = row.get("partner_settings") if isinstance(row.get("partner_settings"), dict) else {}
        has_partner_integration = bool(
            row.get("has_partner_integration")
            or row.get("partner_integrated")
            or partner_settings.get("mapped_event_count")
            or partner_settings.get("data_sharing_status")
        )
        return AdjustApp(
            token=token,
            name=str(row.get("name") or ""),
            package_name=str(row.get("default_store_app_id") or row.get("bundle_id") or ""),
            has_partner_integration=has_partner_integration,
            raw=row,
        )

    @staticmethod
    def _pick_brand_match(brand: str, apps: list[AdjustApp]) -> AdjustApp:
        brand_text = str(brand).strip()
        if not brand_text:
            return apps[0]
        pattern = re.compile(rf"(?<!\d){re.escape(brand_text)}(?!\d)")
        exact = [app for app in apps if pattern.search(app.name)]
        return exact[0] if exact else apps[0]
