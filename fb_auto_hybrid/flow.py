from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from adjust_service import AdjustService
from browser_session import BrowserSession
from meta_api import AndroidProject, BasicSettings, MetaApi, UiRequired
from meta_fetch import MetaPageClient
from monitor_sheet import MonitorSheet
from vest_service import VestService


@dataclass
class FlowContext:
    browser: str
    account: str | None


@dataclass
class AdAuthorizationResult:
    app_id: str
    existing_ids: list[str]
    submitted_ids: list[str]
    added_ids: list[str]
    duplicate_ids: list[str]


@dataclass
class AdRemovalResult:
    app_id: str
    existing_ids: list[str]
    submitted_ids: list[str]
    removed_ids: list[str]
    missing_ids: list[str]


class FlowRunError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        brand_name: str,
        created_app_id: str = "",
        cleanup_message: str = "",
    ) -> None:
        super().__init__(message)
        self.brand_name = brand_name
        self.created_app_id = created_app_id
        self.cleanup_message = cleanup_message


class HybridFlow:
    def __init__(
        self,
        context: FlowContext,
        logger: Callable[[str], None] | None = None,
        manual_confirm: Callable[[], None] | None = None,
    ) -> None:
        self.context = context
        self.session = BrowserSession(browser_name=context.browser, account=context.account)
        self.logger = logger
        self.manual_confirm = manual_confirm

    def log(self, message: str) -> None:
        if self.logger:
            self.logger(message)
        else:
            print(message)

    @staticmethod
    def _adjust_display_name(adjust_brand_name: str, app: VestAppInfo) -> str:
        brand = str(adjust_brand_name or app.brand_name or "").strip()
        product_info = app.raw.get("product_info") if isinstance(app.raw, dict) else {}
        product_name = ""
        if isinstance(product_info, dict):
            product_name = str(product_info.get("app_name") or "").strip()
        if not product_name:
            return brand
        if product_name.lower().startswith(brand.lower()):
            return product_name
        return f"{brand}-{product_name}" if brand else product_name

    def status(self) -> None:
        page = self.session.start()
        try:
            logged_in = self.session.ensure_login(self.log, self.manual_confirm)
            client = MetaPageClient(page)
            api = MetaApi(client)
            self.log(f"logged_in={logged_in}")
            self.log(f"url={page.url}")
            try:
                cookies = self.session.context.cookies(["https://facebook.com", "https://developers.facebook.com"]) if self.session.context else []
                c_user = next((cookie.get("value") for cookie in cookies if cookie.get("name") == "c_user"), "")
                xs_exists = any(cookie.get("name") == "xs" for cookie in cookies)
                self.log(f"facebook_c_user={c_user or '-'}")
                self.log(f"facebook_xs_cookie={'yes' if xs_exists else 'no'}")
            except Exception as exc:
                self.log(f"facebook_cookie_check_failed={exc}")
            self.log("tokens:")
            for key, value in sorted(api.tokens().items()):
                short = value if len(value) <= 16 else value[:8] + "..."
                self.log(f"  {key}: {short}")
        finally:
            self.session.close()

    def _save_android_platform_verified(
        self,
        page: Page,
        basic_url: str,
        app_id: str,
        app_name: str,
        contact_email: str,
        app_url: str,
        android_project: AndroidProject,
    ) -> Page:
        page = self._open(page, basic_url)
        api = self._api_for(page)
        existing_android_platform = api.has_android_google_play_store()
        self.log(
            "    android_mode="
            + ("edit existing Google Play Store" if existing_android_platform else "add Google Play Store")
        )

        def save_android(existing: bool):
            return api.save_basic_settings(
                app_id,
                BasicSettings(
                    app_name=app_name,
                    contact_email=contact_email,
                    privacy_url=app_url,
                    terms_url=app_url,
                    deletion_url=app_url,
                    android_project=android_project,
                    existing_android_platform=existing,
                ),
                action="Save Android platform",
            )

        result = self._with_login_retry(
            page,
            basic_url,
            "Save Android platform",
            lambda: save_android(existing_android_platform),
        )
        self.log(f"    status={result.status} ok={result.ok}")

        page = self._open(page, basic_url)
        api = self._api_for(page)
        if api.has_android_google_play_store():
            self.log("    android_verify=ok")
            return page

        self.log("    android_verify=missing after API save; adding platform via UI")
        api.ensure_android_platform_container()
        result = self._with_login_retry(
            page,
            basic_url,
            "Save Android platform",
            lambda: save_android(False),
        )
        self.log(f"    status={result.status} ok={result.ok}")

        page = self._open(page, basic_url)
        api = self._api_for(page)
        if not api.has_android_google_play_store():
            raise RuntimeError("Android platform was not added by Meta after save.")
        self.log("    android_verify=ok")
        return page

    def run_brand(
        self,
        brand_name: str,
        contact_email: str | None = None,
        activity_class_name: str | None = None,
        publish: bool = True,
        monitor_secret: str | None = None,
        sync_monitor: bool = True,
        adjust_brand_name: str | None = None,
        monitor_email: str | None = None,
    ) -> str:
        self.log(f"[1] Login Vest and load brand: {brand_name}")
        vest = VestService()
        vest.login()
        app = vest.get_brand_app(brand_name)
        self.log(f"    package={app.package_name}")
        self.log(f"    key_hash={app.fb_key_hash[:8]}...")
        self.log(f"    policy_url={app.remarks_url}")
        activity_class_name = activity_class_name or app.deploy_key_hash
        if not activity_class_name:
            raise RuntimeError("Vest did not return Android Activity class name.")
        self.log(f"    activity={activity_class_name}")

        page = self.session.start()
        fb_app_id = ""
        fb_app_secret = monitor_secret or ""
        try:
            if not self.session.ensure_login(self.log, self.manual_confirm):
                raise UiRequired("Meta is not logged in. Finish login in the browser and rerun.")

            api = self._api_for(page)
            if not contact_email:
                if monitor_email and "@" in monitor_email:
                    contact_email = monitor_email
                else:
                    create_url = "https://developers.facebook.com/apps/creation/"
                    page = self._open(page, create_url)
                    api = self._api_for(page)
                    contact_email = api.default_contact_email()
            api = self._api_for(page)
            self.log(f"[2] Create Meta app: {brand_name}")
            fb_app_id = self._create_app_interface_first(page, api, brand_name, contact_email)
            self.log(f"    fb_app_id={fb_app_id}")

            self.log("[3] Save Basic Settings")
            basic_url = f"https://developers.facebook.com/apps/{fb_app_id}/settings/basic/"
            page = self._open(page, basic_url)
            result, page = self._save_basic_settings_with_context_retry(
                page,
                basic_url,
                fb_app_id,
                BasicSettings(
                    app_name=brand_name,
                    contact_email=contact_email,
                    privacy_url=app.remarks_url,
                    terms_url=app.remarks_url,
                    deletion_url=app.remarks_url,
                ),
            )
            self.log(f"    status={result.status} ok={result.ok}")

            self.log("[4] Save Android platform")
            page = self._save_android_platform_verified(
                page,
                basic_url,
                fb_app_id,
                brand_name,
                contact_email,
                app.remarks_url,
                AndroidProject(
                    package_name=app.package_name,
                    activity_class_name=activity_class_name,
                    key_hash=app.fb_key_hash,
                ),
            )

            if publish:
                self.log("[5] Publish app")
                publish_url = f"https://developers.facebook.com/apps/{fb_app_id}/go_live/"
                page = self._open(page, publish_url)
                api = self._api_for(page)
                result = self._with_login_retry(
                    page,
                    publish_url,
                    "Publish app",
                    lambda: api.publish_app(fb_app_id),
                )
                self.log(f"    status={result.status} ok={result.ok}")
                self.log("[6] Accept AMM terms")
                self._accept_amm_terms(page, api, fb_app_id)
            else:
                self.log("[5] Publish skipped")

            if AdjustService.enabled():
                self.log("[7] Sync Adjust Facebook partner")
                adjust_search_name = str(adjust_brand_name or brand_name).strip()
                self.log(f"    adjust_search={adjust_search_name}")
                adjust_result = AdjustService.from_config().ensure_facebook_partner(
                    adjust_search_name,
                    fb_app_id,
                    package_name=app.package_name,
                    app_display_name=self._adjust_display_name(adjust_search_name, app),
                )
                created_events_text = ", ".join(adjust_result.created_events) if adjust_result.created_events else "none"
                mapped_events_text = ", ".join(
                    f"{name}->{target}" for name, target in adjust_result.mapped_events.items()
                )
                self.log(
                    f"    app={adjust_result.app_name} token={adjust_result.app_token} "
                    f"mode={adjust_result.mode} status={adjust_result.status} ok={adjust_result.ok}"
                )
                self.log(f"    created_events={created_events_text}")
                self.log(f"    mapped_events={mapped_events_text}")
            else:
                self.log("[7] Adjust Facebook partner skipped")

            self.log("[8] Update Vest")
            vest.update_fb_app_id(app, fb_app_id)

            if sync_monitor:
                if not fb_app_secret:
                    self.log("[9] Fetch App Secret")
                    fb_app_secret = self._fetch_app_secret(page, api, fb_app_id)
                    if fb_app_secret:
                        self.log(f"    app_secret={fb_app_secret[:6]}****")
                    else:
                        self.log("    app_secret not found; monitor sheet will preserve existing secret if any")
                self.log("[10] Sync monitor sheet")
                MonitorSheet().sync_brand(
                    adjust_brand_name or brand_name,
                    fb_app_id,
                    fb_app_secret or monitor_secret,
                    roxy_email=monitor_email,
                )

            saved = self.session.save_cookies()
            if saved:
                self.log(f"[done] cookies saved: {saved}")
            self.log(f"[done] brand={brand_name} fb_app_id={fb_app_id}")
            return fb_app_id
        except Exception as exc:
            cleanup_message = ""
            if fb_app_id:
                cleanup_message = "保留未发布 App，未删除"
                if sync_monitor:
                    try:
                        MonitorSheet().sync_brand(
                            adjust_brand_name or brand_name,
                            fb_app_id,
                            fb_app_secret or monitor_secret,
                            roxy_email=monitor_email,
                            online_status="",
                        )
                        cleanup_message += "；已同步表格并清空在线状态"
                    except Exception as sync_exc:
                        cleanup_message += f"；表格同步失败: {sync_exc}"
            raise FlowRunError(
                str(exc),
                brand_name=brand_name,
                created_app_id=fb_app_id,
                cleanup_message=cleanup_message,
            ) from exc
        finally:
            self.session.close()

    def run_brand_override(
        self,
        brand_name: str,
        override_brand_name: str,
        contact_email: str | None = None,
        activity_class_name: str | None = None,
        monitor_secret: str | None = None,
        sync_monitor: bool = True,
        adjust_brand_name: str | None = None,
        monitor_email: str | None = None,
    ) -> str:
        self.log(f"[1] Login Vest and load brand: {brand_name}")
        vest = VestService()
        vest.login()
        app = vest.get_brand_app(brand_name)
        self.log(f"    package={app.package_name}")
        self.log(f"    key_hash={app.fb_key_hash[:8]}...")
        self.log(f"    policy_url={app.remarks_url}")
        activity_class_name = activity_class_name or app.deploy_key_hash
        if not activity_class_name:
            raise RuntimeError("Vest did not return Android Activity class name.")
        self.log(f"    activity={activity_class_name}")

        sheet = MonitorSheet()
        override_row = sheet.find_brand(override_brand_name)
        if not override_row:
            raise RuntimeError(f"覆盖目标 {override_brand_name} 不在监控表中，无法编辑已有 App")
        fb_app_id = str(override_row.get("app_id") or "").strip()
        if not fb_app_id:
            raise RuntimeError(f"覆盖目标 {override_brand_name} 表格中没有 App ID")
        target_row = int(override_row.get("row") or 0) or None
        target_online = MonitorSheet.is_online_status(override_row.get("online_status"))
        self.log(
            f"[2] Override existing Meta app: {override_brand_name} -> {brand_name} "
            f"app_id={fb_app_id} online={target_online}"
        )

        page = self.session.start()
        fb_app_secret = monitor_secret or str(override_row.get("app_secret") or "")
        try:
            if not self.session.ensure_login(self.log, self.manual_confirm):
                raise UiRequired("Meta is not logged in. Finish login in the browser and rerun.")

            client = MetaPageClient(page)
            api = MetaApi(client)

            if not contact_email:
                basic_url = f"https://developers.facebook.com/apps/{fb_app_id}/settings/basic/"
                page = self._open(page, basic_url)
                api = self._api_for(page)
                contact_email = api.default_contact_email()

            self.log("[3] Save Basic Settings")
            basic_url = f"https://developers.facebook.com/apps/{fb_app_id}/settings/basic/"
            page = self._open(page, basic_url)
            result, page = self._save_basic_settings_with_context_retry(
                page,
                basic_url,
                fb_app_id,
                BasicSettings(
                    app_name=brand_name,
                    contact_email=contact_email,
                    privacy_url=app.remarks_url,
                    terms_url=app.remarks_url,
                    deletion_url=app.remarks_url,
                ),
            )
            self.log(f"    status={result.status} ok={result.ok}")

            self.log("[4] Save Android platform")
            page = self._save_android_platform_verified(
                page,
                basic_url,
                fb_app_id,
                brand_name,
                contact_email,
                app.remarks_url,
                AndroidProject(
                    package_name=app.package_name,
                    activity_class_name=activity_class_name,
                    key_hash=app.fb_key_hash,
                ),
            )

            if target_online:
                self.log("[5] Publish skipped because sheet online status is online")
            else:
                self.log("[5] Publish app")
                publish_url = f"https://developers.facebook.com/apps/{fb_app_id}/go_live/"
                page = self._open(page, publish_url)
                api = self._api_for(page)
                result = self._with_login_retry(
                    page,
                    publish_url,
                    "Publish app",
                    lambda: api.publish_app(fb_app_id),
                )
                self.log(f"    status={result.status} ok={result.ok}")
                self.log("[6] Accept AMM terms")
                self._accept_amm_terms(page, api, fb_app_id)

            if AdjustService.enabled():
                self.log("[7] Sync Adjust Facebook partner")
                adjust_search_name = str(adjust_brand_name or brand_name).strip()
                self.log(f"    adjust_search={adjust_search_name}")
                adjust_result = AdjustService.from_config().ensure_facebook_partner(
                    adjust_search_name,
                    fb_app_id,
                    package_name=app.package_name,
                    app_display_name=self._adjust_display_name(adjust_search_name, app),
                )
                created_events_text = ", ".join(adjust_result.created_events) if adjust_result.created_events else "none"
                mapped_events_text = ", ".join(
                    f"{name}->{target}" for name, target in adjust_result.mapped_events.items()
                )
                self.log(
                    f"    app={adjust_result.app_name} token={adjust_result.app_token} "
                    f"mode={adjust_result.mode} status={adjust_result.status} ok={adjust_result.ok}"
                )
                self.log(f"    created_events={created_events_text}")
                self.log(f"    mapped_events={mapped_events_text}")
            else:
                self.log("[7] Adjust Facebook partner skipped")

            self.log("[8] Update Vest")
            vest.update_fb_app_id(app, fb_app_id)

            if sync_monitor:
                if not fb_app_secret:
                    self.log("[9] Fetch App Secret")
                    fb_app_secret = self._fetch_app_secret(page, api, fb_app_id)
                    if fb_app_secret:
                        self.log(f"    app_secret={fb_app_secret[:6]}****")
                    else:
                        self.log("    app_secret not found; monitor sheet will preserve existing secret if any")
                self.log("[10] Sync monitor sheet")
                sheet.sync_brand(
                    adjust_brand_name or brand_name,
                    fb_app_id,
                    fb_app_secret or monitor_secret,
                    roxy_email=monitor_email,
                    target_row=target_row,
                )
                duplicate_count = sheet.clear_duplicate_brand_rows(adjust_brand_name or brand_name, target_row)
                if duplicate_count:
                    self.log(f"    cleared_duplicate_rows={duplicate_count}")

            saved = self.session.save_cookies()
            if saved:
                self.log(f"[done] cookies saved: {saved}")
            self.log(f"[done] brand={brand_name} override={override_brand_name} fb_app_id={fb_app_id}")
            return fb_app_id
        except Exception as exc:
            cleanup_message = "保留覆盖目标 App，未删除"
            if sync_monitor:
                try:
                    sheet.sync_brand(
                        adjust_brand_name or brand_name,
                        fb_app_id,
                        fb_app_secret or monitor_secret,
                        roxy_email=monitor_email,
                        online_status="",
                        target_row=target_row,
                    )
                    duplicate_count = sheet.clear_duplicate_brand_rows(adjust_brand_name or brand_name, target_row)
                    cleanup_message += "；已同步表格并清空在线状态"
                    if duplicate_count:
                        cleanup_message += f"；已清理重复行 {duplicate_count} 条"
                except Exception as sync_exc:
                    cleanup_message += f"；表格同步失败: {sync_exc}"
            raise FlowRunError(
                str(exc),
                brand_name=brand_name,
                created_app_id=fb_app_id,
                cleanup_message=cleanup_message,
            ) from exc
        finally:
            self.session.close()

    def _cleanup_failed_app(self, page: Page, app_id: str) -> str:
        self.log(f"[cleanup] delete failed app: {app_id}")
        api = MetaApi(MetaPageClient(page))
        try:
            result = api.delete_app(app_id)
            if self._verify_app_deleted(page, app_id):
                message = f"deleted via API: status={result.status} ok={result.ok}"
                self.log(f"[cleanup] {message}")
                return message
            self.log(f"[cleanup] API returned status={result.status} ok={result.ok}, but app is still visible")
        except Exception as api_exc:
            self.log(f"[cleanup] API delete failed: {api_exc}")

        try:
            self._delete_app_via_ui(page, app_id)
            if not self._verify_app_deleted(page, app_id):
                raise RuntimeError("UI delete clicked, but app is still visible")
            message = "deleted via UI"
            self.log(f"[cleanup] {message}")
            return message
        except Exception as ui_exc:
            message = f"delete failed: {ui_exc}"
            self.log(f"[cleanup] {message}")
            return message

    def _verify_app_deleted(self, page: Page, app_id: str) -> bool:
        try:
            page = self._open(page, "https://developers.facebook.com/apps/")
            if app_id not in page.content():
                return True

            page = self._open(page, f"https://developers.facebook.com/apps/{app_id}/settings/basic/")
            html = page.content().lower()
            deleted_markers = [
                "this app is unavailable",
                "app not found",
                "you don't have permission",
                "content isn't available",
                "应用不可用",
                "找不到",
                "无权",
            ]
            return app_id not in page.url and any(marker in html for marker in deleted_markers)
        except Exception:
            return False

    def _delete_app_via_ui(self, page: Page, app_id: str) -> None:
        page = self._open(page, f"https://developers.facebook.com/apps/{app_id}/settings/advanced/")
        candidates = [
            r"Delete App",
            r"Remove App",
            r"删除应用",
            r"移除应用",
        ]
        for name in candidates:
            try:
                page.get_by_role("button", name=re.compile(name, re.I)).last.click(timeout=5000)
                break
            except Exception:
                try:
                    page.get_by_text(re.compile(name, re.I)).last.click(timeout=5000)
                    break
                except Exception:
                    continue
        else:
            raise RuntimeError("Delete App button not found on Advanced settings page.")

        BrowserSession.wait_soft_networkidle(page, timeout=5000)
        confirm_patterns = [
            r"Delete App",
            r"Delete",
            r"Remove App",
            r"Remove",
            r"删除应用",
            r"删除",
            r"移除应用",
            r"移除",
        ]
        for name in confirm_patterns:
            try:
                page.get_by_role("button", name=re.compile(name, re.I)).last.click(timeout=5000)
                BrowserSession.wait_soft_networkidle(page, timeout=8000)
                return
            except Exception:
                continue
        raise RuntimeError("Delete confirmation button not found.")

    def configure_existing_app(
        self,
        app_id: str,
        app_name: str,
        contact_email: str,
        url: str,
        package_name: str,
        activity_class_name: str,
        key_hash: str,
        publish: bool = False,
    ) -> None:
        page = self.session.start()
        try:
            if not self.session.ensure_login(self.log, self.manual_confirm):
                raise UiRequired("Meta is not logged in.")

            client = MetaPageClient(page)
            api = MetaApi(client)

            self.log("[1] Basic Settings page")
            basic_url = f"https://developers.facebook.com/apps/{app_id}/settings/basic/"
            page = self._open(page, basic_url)
            result, page = self._save_basic_settings_with_context_retry(
                page,
                basic_url,
                app_id,
                BasicSettings(
                    app_name=app_name,
                    contact_email=contact_email,
                    privacy_url=url,
                    terms_url=url,
                    deletion_url=url,
                ),
            )
            self.log(f"    save_basic_settings status={result.status} ok={result.ok}")

            self.log("[2] Android platform")
            page = self._save_android_platform_verified(
                page,
                f"https://developers.facebook.com/apps/{app_id}/settings/basic/",
                app_id,
                app_name,
                contact_email,
                url,
                AndroidProject(
                    package_name=package_name,
                    activity_class_name=activity_class_name,
                    key_hash=key_hash,
                ),
            )

            if publish:
                self.log("[3] Publish page")
                page = self._open(page, f"https://developers.facebook.com/apps/{app_id}/go_live/")
                api = self._api_for(page)
                result = api.publish_app(app_id)
                self.log(f"    publish status={result.status} ok={result.ok}")
                self.log("[4] Accept AMM terms")
                self._accept_amm_terms(page, api, app_id)

            self.session.save_cookies()
        finally:
            self.session.close()

    def authorize_ad_accounts(self, app_id: str, ad_account_ids: list[str]) -> AdAuthorizationResult:
        clean_ids: list[str] = []
        seen_input: set[str] = set()
        for value in ad_account_ids:
            text = str(value).strip()
            if not re.fullmatch(r"\d{6,}", text) or text in seen_input:
                continue
            seen_input.add(text)
            clean_ids.append(text)
        if not clean_ids:
            raise ValueError("No ad account ids provided.")
        page = self.session.start()
        try:
            if not self.session.ensure_login(self.log, self.manual_confirm):
                raise UiRequired("Meta is not logged in.")

            advanced_url = f"https://developers.facebook.com/apps/{app_id}/settings/advanced/"
            self.log(f"[ad-auth] open advanced settings: {app_id}")
            page = self._open(page, advanced_url)
            api = self._api_for(page)
            existing_ids = api.current_authorized_ad_accounts(app_id)
            existing_set = set(existing_ids)
            added_ids = [ad_id for ad_id in clean_ids if ad_id not in existing_set]
            duplicate_ids = [ad_id for ad_id in clean_ids if ad_id in existing_set]
            submitted_ids = existing_ids + added_ids
            self.log(
                f"[ad-auth] existing={len(existing_ids)} incoming={len(clean_ids)} "
                f"add={len(added_ids)} duplicate={len(duplicate_ids)}"
            )
            if not added_ids:
                self.log("[ad-auth] nothing to save; all ids already exist")
                self.session.save_cookies()
                return AdAuthorizationResult(
                    app_id=app_id,
                    existing_ids=existing_ids,
                    submitted_ids=submitted_ids,
                    added_ids=added_ids,
                    duplicate_ids=duplicate_ids,
                )
            result = self._with_login_retry(
                page,
                advanced_url,
                "Save authorized ad accounts",
                lambda: api.save_authorized_ad_accounts(app_id, submitted_ids),
            )
            self.log(
                f"[ad-auth] status={result.status} ok={result.ok} "
                f"submitted={len(submitted_ids)} added={len(added_ids)}"
            )
            self.session.save_cookies()
            return AdAuthorizationResult(
                app_id=app_id,
                existing_ids=existing_ids,
                submitted_ids=submitted_ids,
                added_ids=added_ids,
                duplicate_ids=duplicate_ids,
            )
        finally:
            self.session.close()

    def remove_authorized_ad_accounts(self, app_id: str, ad_account_ids: list[str]) -> AdRemovalResult:
        clean_ids: list[str] = []
        seen_input: set[str] = set()
        for value in ad_account_ids:
            text = str(value).strip()
            if not re.fullmatch(r"\d{6,}", text) or text in seen_input:
                continue
            seen_input.add(text)
            clean_ids.append(text)

        page = self.session.start()
        try:
            if not self.session.ensure_login(self.log, self.manual_confirm):
                raise UiRequired("Meta is not logged in.")

            advanced_url = f"https://developers.facebook.com/apps/{app_id}/settings/advanced/"
            self.log(f"[ad-remove] open advanced settings: {app_id}")
            page = self._open(page, advanced_url)
            api = self._api_for(page)
            existing_ids = api.current_authorized_ad_accounts(app_id)
            if clean_ids:
                remove_set = set(clean_ids)
                removed_ids = [ad_id for ad_id in existing_ids if ad_id in remove_set]
                missing_ids = [ad_id for ad_id in clean_ids if ad_id not in set(existing_ids)]
                submitted_ids = [ad_id for ad_id in existing_ids if ad_id not in remove_set]
            else:
                removed_ids = list(existing_ids)
                missing_ids = []
                submitted_ids = []
            self.log(
                f"[ad-remove] existing={len(existing_ids)} incoming={len(clean_ids)} "
                f"remove={len(removed_ids)} missing={len(missing_ids)}"
            )
            if not removed_ids:
                self.log("[ad-remove] nothing to save; no incoming ids exist")
                self.session.save_cookies()
                return AdRemovalResult(
                    app_id=app_id,
                    existing_ids=existing_ids,
                    submitted_ids=submitted_ids,
                    removed_ids=removed_ids,
                    missing_ids=missing_ids,
                )

            result = self._with_login_retry(
                page,
                advanced_url,
                "Remove authorized ad accounts",
                lambda: api.save_authorized_ad_accounts(app_id, submitted_ids),
            )
            self.log(
                f"[ad-remove] status={result.status} ok={result.ok} "
                f"submitted={len(submitted_ids)} removed={len(removed_ids)}"
            )
            self.session.save_cookies()
            return AdRemovalResult(
                app_id=app_id,
                existing_ids=existing_ids,
                submitted_ids=submitted_ids,
                removed_ids=removed_ids,
                missing_ids=missing_ids,
            )
        finally:
            self.session.close()

    def _create_app_interface_first(self, page: Page, api: MetaApi, brand_name: str, contact_email: str) -> str:
        page = self._open(page, "https://developers.facebook.com/apps/creation/")
        api = self._api_for(page)
        try:
            created = api.create_app(brand_name, contact_email)
            return created.app_id
        except UiRequired as exc:
            self.log(f"    interface create unavailable, fallback to visible UI: {exc}")
            return self._create_app_via_ui(page, brand_name, contact_email)

    def _create_app_via_ui(self, page: Page, brand_name: str, contact_email: str) -> str:
        self.log("    UI fallback: fill create wizard")
        page = self._open(page, "https://developers.facebook.com/apps/creation/")
        page = self.session.require_page()

        if BrowserSession.needs_manual_login(page):
            self.log("    Creation page is still login/checkpoint. Finish it in the browser, then click Confirm Continue.")
            if self.manual_confirm:
                self.manual_confirm()
            else:
                input()
            self.session.save_cookies()
            page = self._open(page, "https://developers.facebook.com/apps/creation/")
            page = self.session.require_page()

        self.log("    filling App name")
        self._fill_first(page, ['input[aria-label="App name"]', 'input[name="basic_name"]'], brand_name)
        self.log("    App name filled")
        self._fill_optional(page, ['input[name="basic_email"]', 'input[type="email"]'], contact_email)
        self._dismiss_new_create_apps_modal(page)
        self.log("    clicking Next after App details")
        self._click_button(page, "Next")

        self.log("    selecting use case")
        page.get_by_text("Create & manage app ads with Meta Ads Manager").click(timeout=20000)
        self.log("    clicking Next after Use cases")
        self._click_button(page, "Next")

        self.log("    selecting no business portfolio")
        try:
            page.get_by_text("I don't want to connect a business portfolio yet.", exact=True).click(timeout=10000)
        except Exception:
            try:
                page.get_by_text("I don\u2019t want to connect a business portfolio yet.", exact=True).click(timeout=10000)
            except Exception:
                page.locator('input[type="radio"]').last.click(timeout=10000)
        self.log("    clicking Next after Business")
        self._click_button(page, "Next")

        self.log("    clicking Next after Requirements")
        self._click_button(page, "Next")
        self._dismiss_new_create_apps_modal(page)
        self.log("    clicking Create app")
        self._click_button(page, "Create app")

        return self._wait_for_created_app_after_possible_verification(page, brand_name)

    def _accept_amm_terms(self, page: Page, api: MetaApi, app_id: str) -> None:
        terms_url = f"https://developers.facebook.com/advanced_mobile_measurement/terms?appid={app_id}"
        page = self._open(page, terms_url)
        api = self._api_for(page)
        auto_clicked = BrowserSession.auto_click_confirmations(page, self.log, max_clicks=2)
        if auto_clicked and not self._amm_accept_button_visible(page):
            self.log("    accepted via auto confirmation")
            return
        try:
            result = self._with_login_retry(
                page,
                terms_url,
                "Accept AMM terms",
                lambda: api.accept_advanced_mobile_measurement_terms(app_id),
            )
            if result:
                self.log(f"    status={result.status} ok={result.ok}")
                return
        except Exception as exc:
            self.log(f"    interface accept unavailable, fallback to visible UI: {exc}")

        if not self._amm_accept_button_visible(page):
            self.log("    already accepted or terms page not required")
            return

        try:
            accept = page.get_by_role("button", name=re.compile(r"Accept|接受|同意", re.I)).first
            accept.wait_for(state="visible", timeout=7000)
            accept.click(timeout=10000)
            BrowserSession.wait_soft_networkidle(page, timeout=8000)
            self.log("    accepted via UI")
        except Exception:
            html = page.content().lower()
            if "accept" not in html and "advanced mobile measurement" not in html:
                self.log("    already accepted or terms page not required")
                return
            raise

    def _fetch_app_secret(self, page: Page, api: MetaApi, app_id: str) -> str:
        basic_url = f"https://developers.facebook.com/apps/{app_id}/settings/basic/"
        try:
            page = self._open(page, basic_url)
            api = self._api_for(page)
            try:
                result = api.request_app_secret(app_id)
                secret = MetaApi.extract_app_secret_from_text(result.text)
                if secret:
                    return secret
            except Exception as exc:
                self.log(f"    request app secret needs confirmation or failed: {exc}")

            BrowserSession.auto_click_confirmations(page, self.log, max_clicks=4)
            deadline = time.time() + 20
            while time.time() < deadline:
                secret = api.extract_app_secret_from_page()
                if secret:
                    return secret
                try:
                    result = api.request_app_secret(app_id)
                    secret = MetaApi.extract_app_secret_from_text(result.text)
                    if secret:
                        return secret
                except Exception:
                    pass
                BrowserSession.auto_click_confirmations(page, self.log, max_clicks=2)
                time.sleep(1)
        except Exception as exc:
            self.log(f"    fetch app secret failed: {exc}")
        return ""

    @staticmethod
    def _amm_accept_button_visible(page: Page) -> bool:
        try:
            button = page.get_by_role("button", name=re.compile(r"Accept|接受|同意", re.I)).first
            return button.is_visible(timeout=1200)
        except Exception:
            return False

    def _wait_for_created_app_after_possible_verification(self, page: Page, brand_name: str) -> str:
        for attempt in range(1, 6):
            BrowserSession.auto_click_confirmations(page, self.log, max_clicks=4)
            timeout = 45 if attempt == 1 else 120
            app_id = self._wait_for_app_id_from_url(page, timeout_seconds=timeout, logger=self.log)
            if app_id:
                return app_id

            try:
                app_id = MetaApi(MetaPageClient(page)).find_app_id_by_name(brand_name) or ""
                if app_id:
                    return app_id
            except Exception:
                pass

            if attempt >= 5:
                break

            clicked = BrowserSession.auto_click_confirmations(page, self.log, max_clicks=4)
            if clicked:
                continue

            self.log(
                f"    Manual Meta verification may still be required "
                f"({attempt}/5). Finish every Continue/password/checkpoint step, then click Confirm Continue."
            )
            if self.manual_confirm:
                self.manual_confirm()
            else:
                input()
            self.session.save_cookies()

        raise UiRequired("App was not created or app_id was not detected after repeated manual confirmations.")

    def _dismiss_new_create_apps_modal(self, page: Page) -> bool:
        try:
            title = page.get_by_text(re.compile(r"new way to create apps with Meta", re.I)).first
            title.wait_for(state="visible", timeout=1800)
        except Exception:
            return False

        self.log("    first-time create-app modal detected; clicking Create app")
        button = page.get_by_role("button", name=re.compile(r"^Create app$", re.I)).last
        button.click(timeout=7000)
        BrowserSession.wait_soft_networkidle(page, timeout=8000)
        time.sleep(0.8)
        return True

    def _open(self, page: Page, url: str) -> Page:
        page = self.session.goto_with_retry(page, url)
        BrowserSession.wait_soft_networkidle(page)
        return page

    @staticmethod
    def _api_for(page: Page) -> MetaApi:
        return MetaApi(MetaPageClient(page))

    def _with_login_retry(self, page: Page, url: str, label: str, func):
        try:
            BrowserSession.auto_click_confirmations(page, self.log, max_clicks=3)
            return func()
        except UiRequired as exc:
            self.log(f"    {label} needs login/checkpoint retry: {exc}")
            clicked = BrowserSession.auto_click_confirmations(page, self.log, max_clicks=4)
            if clicked:
                self.session.save_cookies()
                page = self._open(page, url)
                return func()
            if not self.manual_confirm:
                raise
            self.log("    Finish browser login/checkpoint if needed, then click Confirm Continue.")
            self.manual_confirm()
            self.session.save_cookies()
            page = self._open(page, url)
            return func()
        except RuntimeError as exc:
            text = str(exc)
            if "was not saved by Meta:" in text and "payload" in text:
                try:
                    page = self._open(page, url)
                    api = MetaApi(MetaPageClient(page))
                    visible_error = api.extract_visible_page_errors(wait_ms=1800)
                    if visible_error:
                        raise RuntimeError(f"{label} was not saved by Meta: {visible_error[:800]}") from exc
                except RuntimeError:
                    raise
                except Exception:
                    pass
            raise

    def _save_basic_settings_with_context_retry(
        self,
        page: Page,
        basic_url: str,
        app_id: str,
        settings: BasicSettings,
        *,
        attempts: int = 3,
    ):
        last_error: RuntimeError | None = None
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                self.log(f"    Save Basic Settings retry {attempt}/{attempts}: reopen page and refresh Meta tokens")
                try:
                    page.goto("about:blank", wait_until="domcontentloaded", timeout=10000)
                except Exception:
                    pass
                time.sleep(1.5 * attempt)
                page = self._open(page, basic_url)

            api = self._api_for(page)
            try:
                result = self._with_login_retry(
                    page,
                    basic_url,
                    "Save Basic Settings",
                    lambda: api.save_basic_settings(app_id, settings, action="Save Basic Settings"),
                )
                return result, page
            except RuntimeError as exc:
                if not self._is_meta_reopen_browser_error(exc) or attempt >= attempts:
                    raise
                last_error = exc

        raise last_error or RuntimeError("Save Basic Settings failed after retry.")

    @staticmethod
    def _is_meta_reopen_browser_error(exc: Exception) -> bool:
        text = str(exc or "").lower()
        return (
            "1357004" in text
            or (
                "something went wrong" in text
                and (
                    "re-opening your browser" in text
                    or "reopening your browser" in text
                    or "closing and re-opening" in text
                )
            )
        )

    @staticmethod
    def _fill_first(page: Page, selectors: list[str], value: str) -> None:
        last_error: Exception | None = None

        label_names = ["App name", "应用名称"]
        for label_name in label_names:
            try:
                locator = page.get_by_label(label_name).first
                locator.wait_for(state="visible", timeout=3000)
                locator.fill(value, timeout=5000)
                return
            except Exception as exc:
                last_error = exc

        try:
            locator = page.get_by_role("textbox", name=re.compile("App name|应用名称", re.I)).first
            locator.wait_for(state="visible", timeout=3000)
            locator.fill(value, timeout=5000)
            return
        except Exception as exc:
            last_error = exc

        for selector in selectors:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=3000)
                locator.fill(value, timeout=5000)
                return
            except Exception as exc:
                last_error = exc

        try:
            inputs = page.locator('input:not([type="hidden"]):not([type="email"])')
            count = inputs.count()
            for index in range(count):
                locator = inputs.nth(index)
                if not locator.is_visible(timeout=500):
                    continue
                current = locator.input_value(timeout=1000)
                if current.strip():
                    continue
                locator.fill(value, timeout=5000)
                return
        except Exception as exc:
            last_error = exc

        try:
            page.evaluate(
                """
                (value) => {
                    const inputs = [...document.querySelectorAll('input')]
                        .filter((input) => input.offsetParent !== null && input.type !== 'hidden' && input.type !== 'email');
                    const target = inputs.find((input) => !input.value) || inputs[0];
                    if (!target) throw new Error('visible input not found');
                    target.focus();
                    target.value = value;
                    target.dispatchEvent(new Event('input', {bubbles: true}));
                    target.dispatchEvent(new Event('change', {bubbles: true}));
                }
                """,
                value,
            )
            return
        except Exception as exc:
            last_error = exc
        raise RuntimeError(f"Cannot fill field: {last_error}")

    @staticmethod
    def _fill_optional(page: Page, selectors: list[str], value: str) -> None:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible(timeout=1000):
                    locator.fill(value)
                    return
            except Exception:
                continue

    @staticmethod
    def _click_button(page: Page, name: str) -> None:
        locator = page.get_by_role("button", name=name).last
        locator.wait_for(state="visible", timeout=20000)
        locator.click(timeout=20000)
        time.sleep(0.8)

    @staticmethod
    def _wait_for_app_id_from_url(
        page: Page,
        timeout_seconds: int,
        logger: Callable[[str], None] | None = None,
    ) -> str | None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            BrowserSession.auto_click_confirmations(page, logger, max_clicks=1)
            match = re.search(r"/apps/(\d{10,})/", page.url)
            if match and "creation" not in page.url:
                return match.group(1)
            try:
                page.wait_for_url("**/apps/**", timeout=1500)
            except PlaywrightTimeoutError:
                pass
            except Exception:
                pass
        return None

