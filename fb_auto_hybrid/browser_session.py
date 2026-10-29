from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from config import chrome_path, cookie_path, edge_path, profile_root, project_dir
from roxy_client import RoxyClient, extract_cdp_endpoint, wait_for_cdp_endpoint
from runtime_env import configure_runtime_environment


configure_runtime_environment()


LOGIN_URL_MARKERS = [
    "facebook.com/login",
    "business.facebook.com/business/loginpage",
    "/business/loginpage",
    "checkpoint",
    "two_step_verification",
    "fbauth",
]

LOGIN_TEXT_MARKERS = [
    "Log into Meta for Developers",
    "Continue with Facebook",
    "Log in with a managed Meta account",
]

HARD_VERIFICATION_MARKERS = [
    "two_step_verification",
    "checkpoint",
    "approvals_code",
    "captcha",
    "security code",
    "two-factor",
    "two factor",
    "authentication code",
    "enter code",
    "验证码",
    "安全码",
    "两步验证",
    "双重验证",
    "人机验证",
]

AUTO_CONFIRM_BUTTON_PATTERN = re.compile(
    r"^(Continue(?:\s+as\b.*)?|Confirm|Submit|OK|Okay|Yes|Log in(?:\s+as\b.*)?|Accept|继续|确认|提交|确定|好的|是|登录|接受|同意)$",
    re.I,
)


@dataclass
class BrowserSession:
    browser_name: str = "chrome"
    account: str | None = None
    base_port: int = 9600

    def __post_init__(self) -> None:
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._playwright_cm: Any = None
        self._playwright: Any = None
        self._roxy_client: RoxyClient | None = None
        self._roxy_profile_started = False
        self._roxy_password_loaded = False
        self._roxy_password = ""

    @property
    def profile_name(self) -> str:
        suffix = self.account or "default"
        return f"{self.browser_name}_{suffix}"

    @property
    def port(self) -> int:
        return self.base_port + (sum(ord(ch) for ch in self.profile_name) % 200)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def profile_dir(self) -> Path:
        path = profile_root() / self.profile_name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def start(self) -> Page:
        self._playwright_cm = sync_playwright()
        self._playwright = self._playwright_cm.start()
        endpoint = self._ensure_cdp_endpoint()
        self.browser = self._playwright.chromium.connect_over_cdp(endpoint)
        self.context = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context(no_viewport=True)
        pages = [page for page in self.context.pages if not page.is_closed()]
        if self.browser_name == "roxy":
            time.sleep(1.2)
            self.page = self._pick_existing_page(pages) or self.context.new_page()
        else:
            self.page = pages[-1] if pages else self.context.new_page()
        if self.browser_name != "roxy":
            self.load_cookies()
        return self.page

    def close(self) -> None:
        self._roxy_password = ""
        self._roxy_password_loaded = False
        if self.browser:
            disconnect = getattr(self.browser, "disconnect", None)
            if callable(disconnect):
                disconnect()
            else:
                self.browser.close()
        if self._playwright_cm:
            self._playwright_cm.__exit__(None, None, None)

    def _ensure_cdp_endpoint(self) -> str:
        if self.browser_name == "roxy":
            return self._ensure_roxy_profile()
        self._ensure_browser_process()
        return self.endpoint

    def _ensure_roxy_profile(self) -> str:
        if not self.account:
            raise ValueError("Roxy 模式需要选择一个 profile。")
        client = RoxyClient()
        self._roxy_client = client
        connection_data = client.open_profile(self.account)
        endpoint = extract_cdp_endpoint(connection_data)
        wait_for_cdp_endpoint(endpoint)
        self._roxy_profile_started = True
        return endpoint

    def confirmation_password(self, logger: Callable[[str], None] | None = None) -> str:
        if self.browser_name != "roxy" or not self.account:
            return ""
        if not self._roxy_password_loaded:
            self._roxy_password_loaded = True
            try:
                client = self._roxy_client or RoxyClient()
                profile = client.find_profile(self.account)
                if profile and profile.dir_id == self.account:
                    self._roxy_password = profile.confirmation_password
                if not self._roxy_password and logger:
                    logger("Roxy remark first line is empty or profile was not found; manual password confirmation may be required.")
            except Exception:
                if logger:
                    logger("Could not read the selected Roxy profile remark; manual password confirmation may be required.")
        return self._roxy_password

    def confirm_page(
        self,
        page: Page,
        logger: Callable[[str], None] | None = None,
        max_clicks: int = 5,
    ) -> int:
        self._fill_confirmation_password(page, logger)
        return self.auto_click_confirmations(
            page,
            logger,
            max_clicks=max_clicks,
            prepare_password=lambda: self._fill_confirmation_password(page, logger),
        )

    def _fill_confirmation_password(
        self, page: Page, logger: Callable[[str], None] | None = None
    ) -> None:
        try:
            url = urlsplit(page.url)
            host = url.hostname or ""
            if url.scheme == "https" and (host == "facebook.com" or host.endswith(".facebook.com")):
                inputs = page.locator('input[type="password"]:visible:not([readonly]):not([disabled])')
                if inputs.count() == 1:
                    password_input = inputs.first
                    password = self.confirmation_password(logger)
                    if password and password_input.input_value(timeout=1000) != password:
                        password_input.fill(password, timeout=3000)
                        if logger:
                            logger("Filled Facebook password confirmation from selected Roxy profile remark.")
        except Exception:
            if logger:
                logger("Could not fill Facebook password confirmation from Roxy remark.")

    def confirm_password_followup(
        self, page: Page, logger: Callable[[str], None] | None = None
    ) -> bool:
        # Meta can hide the password field and display a second Confirm step.
        passwords = page.locator('input[type="password"]:visible:not([readonly]):not([disabled])')
        if passwords.count():
            return False
        dialogs = page.get_by_role("dialog").filter(
            has_text=re.compile(r"Confirm with password|re-enter your password|密码|密碼", re.I)
        )
        candidates = []
        for index in range(dialogs.count()):
            dialog = dialogs.nth(index)
            if not dialog.is_visible():
                continue
            buttons = dialog.get_by_role("button", name=re.compile(r"^(Confirm|确认|確認)$", re.I))
            for button_index in range(buttons.count()):
                button = buttons.nth(button_index)
                if button.is_visible() and button.is_enabled():
                    candidates.append(button)
        if len(candidates) != 1 or passwords.count():
            return False
        try:
            candidates[0].click(timeout=3000)
        except Exception:
            if logger:
                logger("Follow-up password confirmation could not be clicked.")
            return False
        if logger:
            logger("Submitted follow-up password confirmation.")
        return True

    def _ensure_browser_process(self) -> None:
        try:
            requests.get(f"{self.endpoint}/json/version", timeout=1)
            return
        except Exception:
            pass

        if self.browser_name == "chrome":
            executable = chrome_path()
        elif self.browser_name == "edge":
            executable = edge_path()
        else:
            raise ValueError(f"Unsupported CDP browser: {self.browser_name}")

        subprocess.Popen(
            [
                executable,
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                requests.get(f"{self.endpoint}/json/version", timeout=1)
                return
            except Exception:
                time.sleep(0.4)
        raise TimeoutError(f"{self.browser_name} CDP port failed to start: {self.endpoint}")

    def cookie_file(self) -> Path:
        return cookie_path(self.browser_name, self.account)

    def load_cookies(self) -> None:
        if not self.context:
            return
        path = self._find_cookie_file()
        if not path.exists() or path.stat().st_size == 0:
            return
        with path.open("r", encoding="utf-8") as f:
            cookies = json.load(f)
        self.context.add_cookies(cookies)

    def _find_cookie_file(self) -> Path:
        primary = self.cookie_file()
        if primary.exists():
            return primary

        suffix = f"_{self.account}" if self.account else ""
        candidates = [
            project_dir().parent / f"fb_cookies_{self.browser_name}{suffix}.json",
            project_dir().parent / f"fb_cookies_{self.browser_name}.json",
            project_dir().parent / "fb_cookies.json",
        ]
        for candidate in candidates:
            if candidate.exists() and candidate.stat().st_size > 0:
                return candidate
        return primary

    def save_cookies(self) -> Path | None:
        if not self.context:
            return None
        if self.browser_name == "roxy":
            return None
        cookies = self.context.cookies()
        c_user = next((c.get("value") for c in cookies if c.get("name") == "c_user"), None)
        path = cookie_path(self.browser_name, c_user or self.account)
        with path.open("w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        return path

    def goto_developers(self) -> Page:
        page = self.require_page()
        page = self.goto_with_retry(page, "https://developers.facebook.com/apps/")
        self.wait_soft_networkidle(page)
        return page

    def goto_app_creation(self) -> Page:
        page = self.require_page()
        page = self.goto_with_retry(page, "https://developers.facebook.com/apps/creation/")
        self.wait_soft_networkidle(page)
        return page

    def require_page(self) -> Page:
        if not self.page or self.page.is_closed():
            if not self.context:
                raise RuntimeError("BrowserSession has not started.")
            pages = [page for page in self.context.pages if not page.is_closed()]
            self.page = self._pick_existing_page(pages) or self.context.new_page()
        return self.page

    @staticmethod
    def _pick_existing_page(pages: list[Page]) -> Page | None:
        if not pages:
            return None
        preferred_domains = (
            "developers.facebook.com",
            "facebook.com",
            "business.facebook.com",
            "suite.adjust.com",
        )
        for page in reversed(pages):
            url = (page.url or "").lower()
            if any(domain in url for domain in preferred_domains):
                return page
        return pages[-1]

    def new_page(self) -> Page:
        if not self.context:
            raise RuntimeError("BrowserSession has not started.")
        self.page = self.context.new_page()
        return self.page

    def goto_with_retry(
        self,
        page: Page,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        timeout: int = 60000,
        attempts: int = 3,
    ) -> Page:
        last_error: Exception | None = None
        current = page
        for attempt in range(1, attempts + 1):
            try:
                if current.is_closed():
                    current = self.new_page()
                current.goto(url, wait_until=wait_until, timeout=timeout)
                self.page = current
                return current
            except Exception as exc:
                last_error = exc
                if not self._is_transient_page_error(exc) or attempt >= attempts:
                    break
                time.sleep(1.0 + attempt * 0.5)
                current = self._replacement_page(current)
        raise last_error or RuntimeError(f"Navigation failed: {url}")

    def _replacement_page(self, old_page: Page | None = None) -> Page:
        if not self.context:
            raise RuntimeError("BrowserSession has not started.")
        pages = [
            page
            for page in self.context.pages
            if not page.is_closed() and page is not old_page
        ]
        replacement = self._pick_existing_page(pages)
        if not replacement:
            try:
                if old_page and not old_page.is_closed():
                    old_page.close()
            except Exception:
                pass
            replacement = self.context.new_page()
        self.page = replacement
        return replacement

    @staticmethod
    def _is_transient_page_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "frame has been detached",
                "target page, context or browser has been closed",
                "target closed",
                "execution context was destroyed",
                "navigation failed because page was closed",
                "net::err_aborted",
            )
        )

    @staticmethod
    def wait_soft_networkidle(page: Page, timeout: int = 10000) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            pass

    @staticmethod
    def needs_manual_login(page: Page) -> bool:
        current_url = page.url.lower()
        if any(marker in current_url for marker in LOGIN_URL_MARKERS):
            return True

        try:
            password_inputs = page.locator('input[type="password"]')
            if password_inputs.count() > 0 and password_inputs.first.is_visible(timeout=1000):
                return True
        except Exception:
            pass

        try:
            for marker in LOGIN_TEXT_MARKERS:
                if page.get_by_text(marker, exact=True).count() > 0:
                    return True
        except Exception:
            pass

        return False

    @staticmethod
    def has_hard_verification(page: Page) -> bool:
        current_url = page.url.lower()
        if any(marker.lower() in current_url for marker in HARD_VERIFICATION_MARKERS):
            return True
        try:
            text = (page.locator("body").inner_text(timeout=1500) or "").lower()
            return any(marker.lower() in text for marker in HARD_VERIFICATION_MARKERS)
        except Exception:
            return False

    @staticmethod
    def _try_select_saved_password(page: Page, log: Callable[[str], None]) -> bool:
        try:
            password = page.locator('input[type="password"]:visible:not([readonly]):not([disabled])').first
            if password.count() <= 0 or not password.is_visible(timeout=800):
                return False
            if password.input_value(timeout=800):
                return False

            password.click(timeout=2000)
            time.sleep(0.8)
            page.keyboard.press("ArrowDown")
            time.sleep(0.2)
            page.keyboard.press("Enter")
            time.sleep(1.5)

            if password.input_value(timeout=1000):
                log("auto selected saved browser password")
                return True

            password.click(timeout=2000)
            time.sleep(0.3)
            page.keyboard.press("Alt+ArrowDown")
            time.sleep(0.5)
            page.keyboard.press("ArrowDown")
            time.sleep(0.2)
            page.keyboard.press("Enter")
            time.sleep(1.5)
            if password.input_value(timeout=1000):
                log("auto selected saved browser password")
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def auto_click_confirmations(
        page: Page,
        logger: Callable[[str], None] | None = None,
        max_clicks: int = 5,
        prepare_password: Callable[[], None] | None = None,
    ) -> int:
        def log(message: str) -> None:
            if logger:
                logger(message)

        clicked = 0
        for _ in range(max_clicks):
            deadline = time.monotonic() + 3.0
            result = {"kind": "none"}
            while time.monotonic() < deadline:
                if BrowserSession.has_hard_verification(page):
                    return clicked
                if prepare_password:
                    prepare_password()
                marker = str(time.monotonic_ns())
                target = page.locator(f'[data-fb-auto-confirm="{marker}"]')
                try:
                    # Locate in the DOM, but dispatch trusted mouse events through
                    # Playwright. Meta's confirmation controls can ignore el.click().
                    result = page.evaluate(
                        """
                        marker => {
                            const visible = el => {
                                const rect = el.getBoundingClientRect();
                                const style = window.getComputedStyle(el);
                                return rect.width > 0 && rect.height > 0 &&
                                    style.display !== 'none' && style.visibility !== 'hidden';
                            };
                            const selector = 'button, input[type="submit"], input[type="button"], a, [role="button"]';
                            const label = el => String(
                                el.tagName === 'INPUT' ? el.value :
                                (el.innerText || el.textContent || el.getAttribute('aria-label') || '')
                            ).replace(/\\s+/g, ' ').trim();
                            const available = (root, pattern) => [...root.querySelectorAll(selector)]
                                .filter(el => visible(el) && !el.disabled &&
                                    el.getAttribute('aria-disabled') !== 'true' && pattern.test(label(el)));
                            const passwords = [...document.querySelectorAll('input[type="password"]')]
                                .filter(el => visible(el) && !el.readOnly && !el.disabled);
                            if (passwords.length) {
                                if (passwords.length !== 1 || !String(passwords[0].value || '').length) {
                                    return {kind: 'password_wait'};
                                }
                                const pattern = /^(Submit|Continue|Confirm|OK|Log in|提交|继续|确认|确定|登录)$/i;
                                const password = passwords[0];
                                const boundary = password.closest('dialog, [role="dialog"], form');
                                let scope = password.parentElement;
                                while (scope) {
                                    const buttons = available(scope, pattern);
                                    if (buttons.length === 1) {
                                        buttons[0].setAttribute('data-fb-auto-confirm', marker);
                                        return {kind: 'password_submitted', label: label(buttons[0])};
                                    }
                                    if (buttons.length > 1 || scope === boundary || scope === document.body) break;
                                    scope = scope.parentElement;
                                }
                                return {kind: 'password_wait'};
                            }
                            const pattern = /^(Continue(?:\\s+as\\b.*)?|Confirm|Submit|OK|Okay|Yes|Log in(?:\\s+as\\b.*)?|Accept|继续|确认|提交|确定|好的|是|登录|接受|同意)$/i;
                            const buttons = available(document, pattern);
                            if (!buttons.length) return {kind: 'none'};
                            buttons[0].setAttribute('data-fb-auto-confirm', marker);
                            return {kind: 'clicked', label: label(buttons[0])};
                        }
                        """,
                        marker,
                    )
                    if result.get("kind") in {"clicked", "password_submitted"}:
                        ready = target.evaluate(
                            """
                            (el, passwordAction) => {
                                if (!el.isConnected || el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
                                const passwords = [...document.querySelectorAll('input[type="password"]')]
                                    .filter(input => {
                                        const rect = input.getBoundingClientRect();
                                        const style = getComputedStyle(input);
                                        return !input.readOnly && !input.disabled && rect.width > 0 && rect.height > 0 &&
                                            style.display !== 'none' && style.visibility !== 'hidden';
                                    });
                                return passwordAction
                                    ? passwords.length === 1 && String(passwords[0].value || '').length > 0
                                    : passwords.length === 0;
                            }
                            """,
                            result["kind"] == "password_submitted",
                        )
                        if ready:
                            target.click(timeout=3000)
                        else:
                            result = {"kind": "password_wait"}
                except Exception:
                    log("Confirmation button could not be clicked; no successful submission recorded.")
                    return clicked
                finally:
                    try:
                        target.evaluate_all("els => els.forEach(el => el.removeAttribute('data-fb-auto-confirm'))")
                    except Exception:
                        pass
                if result.get("kind") in {"clicked", "password_submitted"}:
                    break
                page.wait_for_timeout(100)

            kind = result.get("kind")
            if kind == "password_submitted":
                clicked += 1
                log(f"自动提交已填密码弹窗：{result.get('label') or 'submit'}")
                BrowserSession.wait_soft_networkidle(page, timeout=8000)
                # Do not repeatedly submit the same dialog if Meta rejects it.
                return clicked
            if kind == "clicked":
                clicked += 1
                log(f"自动点击确认按钮：{result.get('label') or 'button'}")
                BrowserSession.wait_soft_networkidle(page, timeout=6000)
                time.sleep(0.8)
                continue
            if kind == "password_wait":
                log("Password confirmation is not ready; no Submit click performed.")
            break
        return clicked

    def ensure_login(
        self,
        logger: Callable[[str], None] | None = None,
        manual_confirm: Callable[[], None] | None = None,
    ) -> bool:
        def log(message: str) -> None:
            if logger:
                logger(message)
            else:
                print(message)

        page = self.goto_developers()
        self.confirm_page(page, log)
        if not self.needs_manual_login(page):
            self.save_cookies()
            return True

        if self.confirm_page(page, log) and not self.needs_manual_login(page):
            self.save_cookies()
            return True

        log("检测到 Meta 登录/验证页。请在浏览器中手动完成，然后点击“确认继续”。")
        if manual_confirm:
            manual_confirm()
        else:
            input()

        page = self.require_page()
        self.wait_soft_networkidle(page)
        self.save_cookies()
        page = self.goto_developers()
        return not self.needs_manual_login(page)
