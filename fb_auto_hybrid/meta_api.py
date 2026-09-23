from __future__ import annotations

import itertools
import json
import re
from html import unescape
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from meta_fetch import FetchResult, MetaPageClient


class UiRequired(RuntimeError):
    """Current step needs browser/manual fallback."""


@dataclass
class AndroidProject:
    package_name: str
    activity_class_name: str
    key_hash: str
    profile_data_id: str | None = None


@dataclass
class BasicSettings:
    app_name: str
    contact_email: str
    privacy_url: str
    terms_url: str
    deletion_url: str
    category: str = "0"
    country: str = "US"
    android_project: AndroidProject | None = None
    existing_android_platform: bool = False


@dataclass
class CreatedApp:
    app_id: str
    raw: str


class MetaApi:
    """Captured Meta frontend endpoints, executed inside the logged-in page."""

    def __init__(self, client: MetaPageClient) -> None:
        self.client = client
        self._req_counter = itertools.count(1)

    def tokens(self) -> dict[str, str]:
        return self.client.extract_tokens()

    def default_contact_email(self) -> str:
        value = self.client.page.evaluate(
            """
            () => {
                const selectors = [
                    'input[name="basic_email"]',
                    'input[type="email"]',
                    'input[aria-label*="email" i]'
                ];
                for (const selector of selectors) {
                    const input = document.querySelector(selector);
                    if (input && input.value && input.value.includes('@')) return input.value;
                }
                const html = document.documentElement.innerHTML;
                const match = html.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/i);
                return match ? match[0] : '';
            }
            """
        )
        if not value:
            raise UiRequired("Cannot find default Meta contact email on the current page.")
        return str(value)

    def has_android_google_play_store(self) -> bool:
        page = self.client.page
        try:
            page.wait_for_timeout(800)
        except Exception:
            pass
        try:
            return bool(
                page.evaluate(
                    """
                    () => {
                        function visible(el) {
                            if (!el) return false;
                            const style = window.getComputedStyle(el);
                            if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
                            const rect = el.getBoundingClientRect();
                            return rect.width > 0 && rect.height > 0;
                        }

                        for (const input of document.querySelectorAll('input[name^="android_package_names"], input[name="android_class_name"]')) {
                            if (visible(input)) return true;
                        }

                        for (const el of document.querySelectorAll('button, [role="tab"], [role="button"], a, div, span')) {
                            const text = String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                            if (text !== 'Google Play Store' || !visible(el)) continue;

                            let node = el;
                            for (let i = 0; i < 8 && node; i += 1, node = node.parentElement) {
                                const sectionText = String(node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
                                if (
                                    sectionText.includes('Package Names') ||
                                    sectionText.includes('Class name') ||
                                    sectionText.includes('Remove App Store')
                                ) {
                                    return true;
                                }
                            }
                        }

                        return false;
                    }
                    """
                )
            )
        except Exception:
            return False

    def ensure_android_platform_container(self) -> bool:
        if self.has_android_google_play_store():
            return False

        page = self.client.page

        def visible_dialog_text() -> str:
            try:
                return str(
                    page.evaluate(
                        """
                        () => {
                            function visible(el) {
                                if (!el) return false;
                                const style = window.getComputedStyle(el);
                                if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
                                const rect = el.getBoundingClientRect();
                                return rect.width > 0 && rect.height > 0;
                            }
                            const dialogs = [...document.querySelectorAll('[role="dialog"]')]
                                .filter(visible)
                                .map(el => String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim());
                            return dialogs.join(' | ');
                        }
                        """
                    )
                )
            except Exception:
                return ""

        def click_text_parent(pattern: str) -> bool:
            try:
                return bool(
                    page.evaluate(
                        """
                        (pattern) => {
                            const regex = new RegExp(pattern, 'i');
                            function visible(el) {
                                if (!el) return false;
                                const style = window.getComputedStyle(el);
                                if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
                                const rect = el.getBoundingClientRect();
                                return rect.width > 0 && rect.height > 0;
                            }
                            const candidates = [...document.querySelectorAll('button, [role="button"], div, span')];
                            for (const el of candidates) {
                                const text = String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                                if (!regex.test(text) || !visible(el)) continue;
                                const target = el.matches('button, [role="button"]') ? el : el.closest('button, [role="button"]');
                                if (target && visible(target)) {
                                    target.click();
                                    return true;
                                }
                            }
                            return false;
                        }
                        """,
                        pattern,
                    )
                )
            except Exception:
                return False

        def click_one(locators, label: str, timeout: int = 10000, text_pattern: str | None = None) -> None:
            last_error: Exception | None = None
            for locator in locators:
                try:
                    item = locator.last
                    item.wait_for(state="visible", timeout=timeout)
                    item.scroll_into_view_if_needed(timeout=timeout)
                    item.click(timeout=timeout)
                    return
                except Exception as exc:
                    last_error = exc
            if text_pattern and click_text_parent(text_pattern):
                return
            raise UiRequired(f"Cannot click {label}: {last_error}")

        def choose_google_play_store() -> None:
            dialog = page.locator('[role="dialog"]')
            click_one(
                [
                    dialog.locator('[role="combobox"]').filter(has_text=re.compile(r"Select Android store|选择", re.I)),
                    dialog.locator('[aria-haspopup="listbox"]'),
                    dialog.locator('input[placeholder*="Android store" i]'),
                    dialog.get_by_text(re.compile(r"Select Android store|选择 Android store|选择安卓商店", re.I)),
                ],
                "Android store dropdown",
                text_pattern=r"Select Android store|选择 Android store|选择安卓商店",
            )
            page.wait_for_timeout(600)
            click_one(
                [
                    page.get_by_role("option", name=re.compile(r"Google Play Store", re.I)),
                    page.locator('[role="option"]').filter(has_text=re.compile(r"Google Play Store", re.I)),
                    page.get_by_text(re.compile(r"^Google Play Store$", re.I)),
                    page.locator('div, span').filter(has_text=re.compile(r"^Google Play Store$", re.I)),
                ],
                "Google Play Store",
                text_pattern=r"^Google Play Store$",
            )
            page.wait_for_timeout(600)

        next_pattern = re.compile(r"^(Next|下一步)$", re.I)
        for _ in range(6):
            if self.has_android_google_play_store():
                return True

            dialog_text = visible_dialog_text()
            dialog = page.locator('[role="dialog"]')

            if re.search(r"Select platform|选择平台", dialog_text, re.I):
                click_one(
                    [
                        dialog.get_by_role("button", name=re.compile(r"^Android(?:\\s+\\d+.*)?$", re.I)),
                        dialog.locator('[role="button"]').filter(has_text=re.compile(r"Android", re.I)),
                        dialog.get_by_text("Android", exact=True),
                    ],
                    "Android",
                    text_pattern=r"^Android(?:\\s+\\d+.*)?$",
                )
                page.wait_for_timeout(500)
                click_one(
                    [
                        dialog.get_by_role("button", name=next_pattern),
                        dialog.locator('button, [role="button"]').filter(has_text=next_pattern),
                    ],
                    "Next after Android",
                    text_pattern=r"^(Next|下一步)$",
                )
                page.wait_for_timeout(900)
                continue

            if re.search(r"Select Android Store|选择 Android Store|选择安卓商店", dialog_text, re.I):
                if not re.search(r"Google Play Store", dialog_text, re.I):
                    choose_google_play_store()
                    dialog_text = visible_dialog_text()
                click_one(
                    [
                        dialog.get_by_role("button", name=next_pattern),
                        dialog.locator('button, [role="button"]').filter(has_text=next_pattern),
                    ],
                    "Next after Android store",
                    text_pattern=r"^(Next|下一步)$",
                )
                page.wait_for_timeout(1200)
                continue

            click_one(
                [
                    page.get_by_role("button", name=re.compile(r"^(Add platform|添加平台)$", re.I)),
                    page.locator('[role="button"]').filter(has_text=re.compile(r"Add platform|添加平台", re.I)),
                    page.get_by_text(re.compile(r"^(Add platform|添加平台)$", re.I)),
                ],
                "Add platform",
                text_pattern=r"^(Add platform|添加平台)$",
            )
            page.wait_for_timeout(800)

        return True

    def common_payload(self) -> dict[str, Any]:
        tokens = self.tokens()
        user = tokens.get("__user") or tokens.get("USER_ID") or tokens.get("user_id") or tokens.get("actorID")
        payload = {
            "__user": user,
            "__a": tokens.get("__a", "1"),
            "__req": self._next_req(),
            "__hs": tokens.get("__hs", ""),
            "dpr": tokens.get("dpr", "1"),
            "__ccg": tokens.get("__ccg", "UNKNOWN"),
            "__rev": tokens.get("__rev") or tokens.get("__spin_r", ""),
            "__s": tokens.get("__s", ""),
            "__hsi": tokens.get("__hsi", ""),
            "__dyn": tokens.get("__dyn", ""),
            "fb_dtsg": tokens.get("fb_dtsg") or tokens.get("token", ""),
            "jazoest": tokens.get("jazoest", ""),
            "lsd": tokens.get("lsd", ""),
        }
        return {key: value for key, value in payload.items() if value not in (None, "")}

    def _next_req(self) -> str:
        value = next(self._req_counter)
        alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
        if value < len(alphabet):
            return alphabet[value]
        return str(value)

    def call_captured_form(self, url: str, payload: dict[str, Any], action: str = "Meta request") -> FetchResult:
        result = self.client.fetch_form(url, {**payload, **self.common_payload()})
        self.raise_if_failed(result, action)
        return result

    def raise_if_failed(self, result: FetchResult, action: str) -> None:
        text = result.text or ""
        lower = text.lower()
        if not result.ok:
            raise RuntimeError(f"{action} failed: HTTP {result.status}, body={text[:500]}")

        parsed = MetaApi._parse_meta_json(text)
        if parsed:
            top_error = parsed.get("error")
            if top_error:
                error_text = " | ".join(
                    str(part)
                    for part in [
                        top_error,
                        parsed.get("errorSummary"),
                        parsed.get("errorDescription"),
                    ]
                    if part
                )
                error_lower = error_text.lower()
                if any(
                    marker in error_lower
                    for marker in ("login", "checkpoint", "two_step", "authentication", "confirmation required")
                ):
                    raise UiRequired(f"{action} requires browser-side confirmation/UI flow: {error_text[:500]}")
                raise RuntimeError(f"{action} was rejected by Meta: {error_text[:800]}")

            payload = parsed.get("payload")
            if isinstance(payload, dict):
                if payload.get("success") is True:
                    return
                error = payload.get("error") or payload.get("errorSummary") or payload.get("errorDescription")
                if error:
                    error_text = str(error)
                    error_lower = error_text.lower()
                    if any(
                        marker in error_lower
                        for marker in ("login", "checkpoint", "two_step", "authentication", "confirmation required")
                    ):
                        raise UiRequired(f"{action} requires browser-side confirmation/UI flow: {error_text[:500]}")
                    raise RuntimeError(f"{action} was rejected by Meta: {error_text[:800]}")
                if payload.get("success") is False:
                    detail = (
                        MetaApi._extract_meta_error_text(parsed)
                        or self.extract_visible_page_errors()
                        or text[:800]
                    )
                    raise RuntimeError(f"{action} was not saved by Meta: {detail[:800]}")
            return

        login_markers = [
            "log into meta for developers",
            "continue with facebook",
            "two_step_verification",
            "checkpoint",
            "business/loginpage",
        ]
        if any(marker in lower for marker in login_markers):
            raise UiRequired(f"{action} returned a login/checkpoint page.")

        policy_markers = [
            "policy violations",
            "cannot be added",
            "not allowed",
            "invalid package",
        ]
        if any(marker in lower for marker in policy_markers):
            raise RuntimeError(f"{action} was rejected by Meta: {text[:800]}")

    @staticmethod
    def _parse_meta_json(text: str) -> dict[str, Any] | None:
        stripped = (text or "").strip()
        if stripped.startswith("for (;;);"):
            stripped = stripped[len("for (;;);") :].strip()
        if not stripped.startswith("{"):
            return None
        try:
            value = json.loads(stripped)
        except Exception:
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _extract_meta_error_text(parsed: dict[str, Any]) -> str:
        pieces: list[str] = []
        seen: set[str] = set()

        def add_html(html_text: str) -> None:
            text = re.sub(r"<[^>]+>", " ", html_text)
            text = (
                text.replace("\\u003C", "<")
                .replace("\\u003E", ">")
                .replace("\\/", "/")
            )
            text = re.sub(r"\s+", " ", text).strip()
            if text and text not in seen:
                seen.add(text)
                pieces.append(text)

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "__html" and isinstance(item, str):
                        add_html(item)
                    elif key.lower() in {"errormessage", "errormessageid", "errorsummary", "errordescription"}:
                        if isinstance(item, str):
                            add_html(item)
                    else:
                        walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(parsed)
        return " | ".join(pieces)

    def extract_visible_page_errors(self, wait_ms: int = 1200) -> str:
        page = self.client.page
        try:
            page.wait_for_timeout(wait_ms)
        except Exception:
            pass
        try:
            return str(
                page.evaluate(
                    """
                    () => {
                        const selectors = [
                            '[role="alert"]',
                            '[aria-invalid="true"]',
                            '[data-errormessage-id]',
                            '[id*="error" i]',
                            '[class*="error" i]',
                            '[class*="Error" i]',
                            '[data-testid*="error" i]',
                            'div[style*="rgb(255"]',
                            'span[style*="rgb(255"]'
                        ];
                        const texts = [];
                        const seen = new Set();

                        function visible(el) {
                            if (!el) return false;
                            const style = window.getComputedStyle(el);
                            if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
                            const rect = el.getBoundingClientRect();
                            return rect.width > 0 && rect.height > 0;
                        }

                        function add(text) {
                            text = String(text || '').replace(/\\s+/g, ' ').trim();
                            if (!text || text.length < 3 || seen.has(text)) return;
                            seen.add(text);
                            texts.push(text);
                        }

                        for (const selector of selectors) {
                            for (const el of document.querySelectorAll(selector)) {
                                if (!visible(el)) continue;
                                add(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title'));
                                const describedBy = el.getAttribute('aria-describedby') || el.getAttribute('data-errormessage-id');
                                if (describedBy) {
                                    for (const id of describedBy.split(/\\s+/)) {
                                        const target = document.getElementById(id);
                                        if (target) add(target.innerText || target.textContent);
                                    }
                                }
                                const parent = el.closest('label, div, section, form');
                                if (parent && parent !== el) add(parent.innerText || parent.textContent);
                            }
                        }

                        const keywords = [
                            'policy violation',
                            'cannot be added',
                            'Please correct the highlighted fields',
                            'Invalid Privacy Policy URL',
                            'required',
                            'not valid',
                            'error',
                            '违规',
                            '无法添加',
                            '请更正',
                            '无效',
                            '必填',
                        ];
                        const bodyText = document.body ? document.body.innerText : '';
                        for (const line of bodyText.split('\\n')) {
                            const text = line.replace(/\\s+/g, ' ').trim();
                            if (keywords.some((keyword) => text.toLowerCase().includes(keyword.toLowerCase()))) {
                                add(text);
                            }
                        }

                        return texts.slice(0, 8).join(' | ');
                    }
                    """
                )
            ).strip()
        except Exception:
            return ""

    def create_app(self, app_name: str, contact_email: str, use_case: str = "APP_INSTALL_ADS") -> CreatedApp:
        url = "https://developers.facebook.com/apps/async/create/?app_party=third&app_owner_type=0"
        payload = {
            "basic_name": app_name,
            "basic_email": contact_email,
            "business": "",
            "oncall_short_name": "",
            "app_use_cases[0]": use_case,
            "app_use_case_business_use": "",
            "justification_for_app_creation": "",
            "is_internal_test_app": "false",
            "internal_test_app_restriction_bypass": "false",
            "is_shell_app": "false",
        }
        result = self.call_captured_form(url, payload, "Create app")
        app_id = self._extract_created_app_id(result.text) or self._extract_app_id(result.text)
        if not app_id:
            app_id = self.find_app_id_by_name(app_name)
        if not app_id:
            raise UiRequired("Create app did not return app_id; use browser fallback.")
        return CreatedApp(app_id=app_id, raw=result.text)

    def save_basic_settings(
        self,
        app_id: str,
        settings: BasicSettings,
        action: str = "Save basic settings",
    ) -> FetchResult:
        url = f"https://developers.facebook.com/x/apps/{app_id}/settings/basic/save/"
        payload = {
            "basic_name": settings.app_name,
            "basic_namespace": "",
            "basic_email": settings.contact_email,
            "app_details_privacy_policy_url": settings.privacy_url,
            "app_details_terms_of_service_url": settings.terms_url,
            "user_data_deletion_type": "user_data_deletion_instructions",
            "app_details_user_data_deletion": settings.deletion_url,
            "app_details_category": settings.category,
            "name": "",
            "email": "",
            "address_street": "",
            "address_street_2": "",
            "address_city": "",
            "address_state": "",
            "address_zipcode": "",
            "address_country": settings.country,
            "site_url": "",
            "ios_iphone_store_id": "",
            "ios_ipad_store_id": "",
            "ios_purchase_secret": "",
            "ios_implicit_purchase_events": "1",
            "android_stores_settings": "{}",
            "android_class_name": "",
            "android_implicit_purchase_events": "true",
            "page_tab_secure_url": "",
            "page_tab_name": "",
            "page_tab_edit_url": "",
            "page_tab_admin_control": "1",
            "page_tab_wide": "1",
            "windows_10_store_url": "",
            "windows_store_id": "",
            "windows_phone_store_id": "",
            "save_changes": "1",
            "__aaid": "0",
        }
        if settings.android_project:
            android_payload = {
                "android_stores_settings": '{"google_play":{}}',
                "android_package_names[0]": settings.android_project.package_name,
                "android_class_name": settings.android_project.activity_class_name,
                "android_key_hashes[0]": settings.android_project.key_hash,
                "android_stores[0]": "google_play",
            }
            if not settings.existing_android_platform:
                android_payload["platforms[0]"] = "android"
            payload.update(android_payload)
        return self.call_captured_form(url, payload, action)

    def load_android_setup(self, app_id: str) -> FetchResult:
        url = f"https://developers.facebook.com/apps/{app_id}/platform-setup/loader/?platform=android"
        return self.call_captured_form(url, {}, "Load Android setup")

    def save_android_package_class(self, app_id: str, package_name: str, activity_class_name: str) -> FetchResult:
        url = f"https://developers.facebook.com/apps/async/platform-setup/android/packclass/save/?app_id={app_id}"
        payload = {
            "android_package_name": package_name,
            "android_class_name": activity_class_name,
        }
        return self.call_captured_form(url, payload, "Save Android package/class")

    def save_android_key_hashes(self, app_id: str, key_hash: str, profile_data_id: str | None = None) -> FetchResult:
        if not profile_data_id:
            profile_data_id = self.extract_profile_data_id_from_page()
        if not profile_data_id:
            raise UiRequired("profile_data_id not found; open Android Quick Start page first.")
        url = (
            "https://developers.facebook.com/apps/async/platform-setup/android/keyhashes/save/"
            f"?app_id={app_id}&profile_data_id={profile_data_id}"
        )
        payload = {"android_key_hashes[0]": key_hash}
        return self.call_captured_form(url, payload, "Save Android key hash")

    def configure_android_project(self, app_id: str, project: AndroidProject) -> list[FetchResult]:
        loader_result = self.load_android_setup(app_id)
        profile_data_id = project.profile_data_id or self.extract_profile_data_id_from_text(loader_result.text)
        package_result = self.save_android_package_class(app_id, project.package_name, project.activity_class_name)
        profile_data_id = profile_data_id or self.extract_profile_data_id_from_text(package_result.text)
        profile_data_id = profile_data_id or self.extract_profile_data_id_from_page()
        return [
            loader_result,
            package_result,
            self.save_android_key_hashes(app_id, project.key_hash, profile_data_id),
        ]

    def publish_app(self, app_id: str) -> FetchResult:
        url = f"https://developers.facebook.com/apps/{app_id}/async/review/visibility-toggle/?visible=true"
        payload = {"__aaid": "0"}
        return self.call_captured_form(url, payload, "Publish app")

    def accept_advanced_mobile_measurement_terms(self, app_id: str) -> FetchResult | None:
        page = self.client.page
        info = page.evaluate(
            """
            () => {
                const buttons = [...document.querySelectorAll('button, input[type="submit"], a')]
                    .filter((el) => /accept/i.test(el.innerText || el.value || el.textContent || ''));
                const button = buttons[0] || null;
                const form = button ? button.closest('form') : document.querySelector('form');
                const data = {};
                if (form) {
                    for (const input of form.querySelectorAll('input[name]')) {
                        const type = (input.getAttribute('type') || '').toLowerCase();
                        if ((type === 'checkbox' || type === 'radio') && !input.checked) continue;
                        data[input.name] = input.value || '1';
                    }
                }
                return {
                    hasAccept: Boolean(button),
                    action: form ? form.action : '',
                    method: form ? (form.method || 'POST') : 'POST',
                    data,
                };
            }
            """
        )
        if not info.get("hasAccept"):
            return None

        action = str(info.get("action") or "")
        if action:
            payload = dict(info.get("data") or {})
            payload.setdefault("appid", app_id)
            payload.setdefault("app_id", app_id)
            payload.setdefault("__aaid", "0")
            result = self.client.fetch_form(action, {**payload, **self.common_payload()}, method="POST")
            self.raise_if_failed(result, "Accept Advanced Mobile Measurement terms")
            return result
        return None

    def load_advanced_settings(self, app_id: str) -> FetchResult:
        url = "https://developers.facebook.com/ajax/pagelet/generic.php/DeveloperAppDashboardContentPagelet"
        payload = self.common_payload()
        payload.update(
            {
                "fb_dtsg_ag": payload.get("fb_dtsg", ""),
                "data": json.dumps(
                    {
                        "app_id": str(app_id),
                        "page": "settings",
                        "tab": "advanced",
                        "app_locale": None,
                        "id": None,
                        "a_n": None,
                        "c_n": None,
                        "alert_id": None,
                        "ref": None,
                        "dev_listing_id": None,
                        "use_case_enum": None,
                        "add_on_enum": None,
                        "is_go_live_modal_shown": False,
                        "show_uba_opt_in": False,
                    },
                    separators=(",", ":"),
                ),
                "__aaid": "0",
            }
        )
        payload = {key: value for key, value in payload.items() if value not in (None, "")}
        result = self.client.fetch_form(f"{url}?{urlencode(payload)}", {}, method="GET")
        self.raise_if_failed(result, "Load advanced settings")
        return result

    def current_authorized_ad_accounts(self, app_id: str) -> list[str]:
        ids = self.extract_authorized_ad_account_ids_from_page()
        try:
            result = self.load_advanced_settings(app_id)
            ids.extend(self.extract_authorized_ad_account_ids_from_text(result.text))
        except Exception:
            pass
        return self._unique_ids(ids)

    def save_authorized_ad_accounts(self, app_id: str, ad_account_ids: list[str]) -> FetchResult:
        url = f"https://developers.facebook.com/x/apps/{app_id}/settings/advanced/save/"
        payload: dict[str, Any] = {
            "auth_is_native": "0",
            "auth_auth_callback_url": "",
            "app_self_upgrade_version": "unversioned",
            "app_roles_upgrade_version": "unversioned",
            "restriction_alcoholic_content": "0",
            "restriction_social_discovery": "1",
            "restriction_by_age": "13+",
            "restriction_by_country": "0",
            "update_notification_email": "",
            "appsecret_proof": "0",
            "require_two_fac_reauth": "0",
            "app_config_api_access": "0",
            "allow_share_redirects": "0",
            "__aaid": "0",
        }
        for index, ad_account_id in enumerate(ad_account_ids):
            payload[f"advertiser_account_ids[{index}]"] = ad_account_id
        return self.call_captured_form(url, payload, "Save authorized ad accounts")

    def extract_authorized_ad_account_ids_from_page(self) -> list[str]:
        try:
            values = self.client.page.evaluate(
                """
                () => {
                    const ids = [];
                    for (const input of document.querySelectorAll('input[name^="advertiser_account_ids"]')) {
                        const value = String(input.value || '').trim();
                        if (/^\\d{6,}$/.test(value)) ids.push(value);
                    }
                    return ids;
                }
                """
            )
            return self._unique_ids(values if isinstance(values, list) else [])
        except Exception:
            return []

    @staticmethod
    def extract_authorized_ad_account_ids_from_text(text: str) -> list[str]:
        decoded = unescape(str(text or ""))
        decoded = decoded.replace("\\/", "/").replace('\\"', '"').replace("\\u0025", "%")
        ids: list[str] = []
        patterns = [
            r'name=["\']advertiser_account_ids\[\d+\]["\'][^>]{0,500}?value=["\'](\d{6,})["\']',
            r'value=["\'](\d{6,})["\'][^>]{0,500}?name=["\']advertiser_account_ids\[\d+\]["\']',
            r'advertiser_account_ids\[\d+\][^0-9]{1,120}(\d{6,})',
        ]
        for pattern in patterns:
            ids.extend(re.findall(pattern, decoded))
        return MetaApi._unique_ids(ids)

    @staticmethod
    def _unique_ids(values: list[Any]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            text = str(value or "").strip()
            if not re.fullmatch(r"\d{6,}", text) or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    def delete_app(self, app_id: str) -> FetchResult:
        urls = [
            f"https://developers.facebook.com/apps/{app_id}/async/delete/",
            f"https://developers.facebook.com/apps/{app_id}/settings/advanced/async/delete/",
            f"https://developers.facebook.com/apps/async/delete/?app_id={app_id}",
        ]
        payload = {
            "__aaid": "0",
            "app_id": app_id,
            "confirmed": "1",
            "delete_app": "1",
        }
        last_error: Exception | None = None
        for url in urls:
            try:
                return self.call_captured_form(url, payload, "Delete app")
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"Delete app failed on all known endpoints: {last_error}")

    def request_app_secret(self, app_id: str, ajax_password: str | None = None) -> FetchResult:
        url = f"https://developers.facebook.com/apps/{app_id}/async/settings/app-secret/"
        payload: dict[str, Any] = {"__aaid": "0"}
        if ajax_password:
            payload["ajax_password"] = ajax_password
            payload["confirmed"] = "1"
        return self.call_captured_form(url, payload, "Request app secret")

    @staticmethod
    def extract_app_secret_from_text(text: str) -> str:
        decoded = unescape(str(text or ""))
        decoded = decoded.replace("\\/", "/").replace('\\"', '"')
        patterns = [
            r'"app_secret"\s*:\s*"([a-fA-F0-9]{32})"',
            r'"app_secret_value"\s*:\s*"([a-fA-F0-9]{32})"',
            r'\b([a-fA-F0-9]{32})\b',
        ]
        for pattern in patterns:
            match = re.search(pattern, decoded)
            if match:
                return match.group(1)
        return ""

    def extract_app_secret_from_page(self) -> str:
        page = self.client.page
        try:
            secret = page.evaluate(
                """
                () => {
                    for (const input of document.querySelectorAll('input')) {
                        const value = String(input.value || '').trim();
                        if (/^[a-fA-F0-9]{32}$/.test(value)) return value;
                    }
                    const html = document.documentElement.innerHTML || '';
                    const match = html.match(/\\b[a-fA-F0-9]{32}\\b/);
                    return match ? match[0] : '';
                }
                """
            )
            return str(secret or "").strip()
        except Exception:
            return ""

    def extract_profile_data_id_from_page(self) -> str | None:
        return self.extract_profile_data_id_from_text(self.client.page.content())

    @staticmethod
    def extract_profile_data_id_from_text(html: str) -> str | None:
        patterns = [
            r"profile_data_id[=/](\d+)",
            r"profile_data_id=(\d+)",
            r'"profile_data_id"\s*:\s*"?(\d+)"?',
            r'\\"profile_data_id\\"\s*:\s*\\"?(\d+)\\"?',
            r'profileDataID"\s*:\s*"?(\d+)"?',
            r'profileDataID\\?"\s*:\s*\\?"?(\d+)\\?"?',
            r"profileDataID[=/](\d+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                return match.group(1)
        return None

    def find_app_id_by_name(self, app_name: str) -> str | None:
        page = self.client.page
        try:
            page.goto("https://developers.facebook.com/apps/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        html = page.content()
        escaped = re.escape(app_name)
        patterns = [
            rf"/apps/(\d+)/[^\"'<>]{{0,200}}{escaped}",
            rf"{escaped}[\s\S]{{0,500}}/apps/(\d+)/",
            rf'"id"\s*:\s*"(\d{{10,}})"[\s\S]{{0,500}}{escaped}',
        ]
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _extract_app_id(text: str) -> str | None:
        patterns = [
            r"/apps/(\d+)/",
            r'"app_id"\s*:\s*"?(\d+)"?',
            r'"id"\s*:\s*"?(\d{10,})"?',
            r"app_id=(\d+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _extract_created_app_id(text: str) -> str | None:
        parsed = MetaApi._parse_meta_json(text)
        if not parsed:
            return None
        payload = parsed.get("payload")
        if not isinstance(payload, dict):
            return None
        extra = payload.get("extra")
        if not isinstance(extra, dict):
            return None
        new_app = extra.get("newApp")
        if not isinstance(new_app, dict):
            return None
        app_id = new_app.get("id")
        return str(app_id) if app_id else None
