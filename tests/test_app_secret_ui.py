import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fb_auto_hybrid"))

from playwright.sync_api import Error, sync_playwright
from browser_session import BrowserSession
from flow import FlowContext, HybridFlow
from meta_api import MetaApi, UiRequired
from meta_fetch import MetaPageClient
from roxy_client import RoxyProfile


SECRET = "ab" * 16
HTML = """
<section><label>Unrelated hash</label><input value="cccccccccccccccccccccccccccccccc">
<button onclick="window.wrongShow = true">Show</button></section>
<section><span id="secret-label">App Secret</span>
<input id="secret-field" type="password" aria-labelledby="secret-label" readonly value="********">
<button onclick="document.querySelector('dialog').showModal()">Show</button></section>
<dialog><h2>Please re-enter your password</h2>
<label for="password">Password</label><input id="password" type="password">
<button type="submit" value="1" data-testid="sec_ac_button" onclick="submitPassword()">Submit</button><div id="error"></div></dialog>
<script>
window.submissions = 0;
function submitPassword() {
    window.submissions++;
    if (document.querySelector('#password').value === 'demo-password') {
        document.querySelector('#secret-field').value = 'abababababababababababababababab';
        document.querySelector('dialog').close();
    } else {
        document.querySelector('#error').textContent = 'Incorrect password';
    }
}
</script>
"""


class SecretParserTests(unittest.TestCase):
    def test_named_secret_only(self):
        self.assertEqual(MetaApi.extract_app_secret_from_text(json.dumps({"app_secret": SECRET})), SECRET)
        self.assertEqual(MetaApi.extract_app_secret_from_text(json.dumps({"app_secret_value": SECRET})), SECRET)
        self.assertEqual(MetaApi.extract_app_secret_from_text(json.dumps({"request_id": SECRET})), "")
        self.assertEqual(MetaApi.extract_app_secret_from_text('<input value="' + SECRET + '">'), "")


class SecretBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)
        except Error as exc:
            cls.playwright.stop()
            raise unittest.SkipTest("Local Chrome unavailable for offline UI test") from exc

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=HTML))
        self.page = self.context.new_page()
        self.page.goto("https://developers.facebook.com/apps/123/settings/basic/")
        self.api = MetaApi(MetaPageClient(self.page))

    def tearDown(self):
        self.context.close()

    def make_flow(self, password):
        self.logs = []
        flow = HybridFlow(FlowContext("roxy", "selected"), logger=self.logs.append)
        flow._open = Mock(return_value=self.page)
        flow._api_for = Mock(return_value=self.api)
        flow.session._roxy_client = SimpleNamespace(find_profile=Mock(return_value=
            RoxyProfile("selected", "account", remark=password + "\nignored data")))
        self.api.request_app_secret = Mock(side_effect=[UiRequired("confirmation"), UiRequired("confirmation")])
        return flow

    def test_reveal_is_scoped_and_unrelated_hash_ignored(self):
        self.assertEqual(self.api.extract_app_secret_from_page(), "")
        self.assertTrue(self.api.show_app_secret())
        self.assertIsNone(self.page.evaluate("window.wrongShow"))
        self.assertTrue(self.page.locator("dialog").is_visible())
        self.page.get_by_label("Password", exact=True).fill("demo-password")
        self.page.get_by_role("button", name="Submit", exact=True).click()
        self.assertEqual(self.api.extract_app_secret_from_page(), SECRET)

    def test_interface_confirmation_failure_falls_back_to_show_and_password(self):
        flow = self.make_flow("demo-password")
        with patch("flow.time.sleep"):
            self.assertEqual(flow._fetch_app_secret(self.page, self.api, "123"), SECRET)
        self.assertEqual(self.page.evaluate("window.submissions"), 1)
        self.assertEqual(self.api.request_app_secret.call_count, 2)
        self.assertNotIn("demo-password", " ".join(self.logs))
        self.assertNotIn(SECRET, " ".join(self.logs))

    def test_rejected_password_is_not_repeatedly_submitted(self):
        flow = self.make_flow("wrong-demo-password")
        with patch("flow.time.sleep"), patch("flow.time.time", side_effect=[0, 1, 2, 31]):
            self.assertEqual(flow._fetch_app_secret(self.page, self.api, "123"), "")
        self.assertEqual(self.page.evaluate("window.submissions"), 1)
        self.assertNotIn("wrong-demo-password", " ".join(self.logs))

    def test_delayed_dialog_gets_note_password_before_submit(self):
        flow = self.make_flow("demo-password")
        self.page.evaluate("setTimeout(() => document.querySelector('dialog').showModal(), 2200)")
        self.assertEqual(flow.session.confirm_page(self.page, self.logs.append, max_clicks=1), 1)
        self.assertEqual(self.page.evaluate("window.submissions"), 1)
        self.assertEqual(self.api.extract_app_secret_from_page(), SECRET)
        flow.session._roxy_client.find_profile.assert_called_once_with("selected")

    def test_empty_note_never_submits_empty_password(self):
        flow = self.make_flow("")
        self.api.show_app_secret()
        with patch.object(BrowserSession, "_try_select_saved_password") as keyboard_fallback:
            self.assertEqual(flow.session.confirm_page(self.page, self.logs.append, max_clicks=4), 0)
            keyboard_fallback.assert_not_called()
        self.assertEqual(self.page.evaluate("window.submissions"), 0)
        self.assertEqual(self.page.get_by_label("Password", exact=True).input_value(), "")

    def test_password_is_prepared_again_after_continue(self):
        flow = self.make_flow("demo-password")
        self.page.evaluate("""() => {
            const button = document.createElement('button');
            button.textContent = 'Continue';
            button.onclick = () => {
                button.remove();
                setTimeout(() => document.querySelector('dialog').showModal(), 1200);
            };
            document.body.appendChild(button);
        }""")
        self.assertEqual(flow.session.confirm_page(self.page, self.logs.append, max_clicks=2), 2)
        self.assertEqual(self.page.evaluate("window.submissions"), 1)
        self.assertEqual(self.api.extract_app_secret_from_page(), SECRET)

    def test_ambiguous_password_fields_block_confirmation(self):
        flow = self.make_flow("demo-password")
        self.api.show_app_secret()
        self.page.evaluate("""() => {
            const extra = document.createElement('input');
            extra.type = 'password';
            document.querySelector('dialog').appendChild(extra);
        }""")
        self.assertEqual(flow.session.confirm_page(self.page, self.logs.append, max_clicks=1), 0)
        self.assertEqual(self.page.evaluate("window.submissions"), 0)
        flow.session._roxy_client.find_profile.assert_not_called()

    def test_normal_confirmation_without_password_still_works(self):
        self.page.set_content('<button onclick="window.accepted=true">Accept</button>')
        self.assertEqual(BrowserSession.auto_click_confirmations(self.page, max_clicks=1), 1)
        self.assertTrue(self.page.evaluate("window.accepted"))

    def test_password_confirm_uses_trusted_click(self):
        flow = self.make_flow("demo-password")
        self.page.evaluate("""() => {
            const oldButton = document.querySelector('[data-testid="sec_ac_button"]');
            const button = document.createElement('div');
            button.setAttribute('role', 'button');
            button.tabIndex = 0;
            button.textContent = 'Confirm';
            button.addEventListener('click', event => {
                window.confirmClickTrusted = event.isTrusted;
                if (event.isTrusted) submitPassword();
            });
            oldButton.replaceWith(button);
        }""")
        self.api.show_app_secret()
        self.assertEqual(flow.session.confirm_page(self.page, self.logs.append, max_clicks=1), 1)
        self.assertTrue(self.page.evaluate("window.confirmClickTrusted"))
        self.assertEqual(self.api.extract_app_secret_from_page(), SECRET)
        self.assertEqual(self.page.locator('[data-fb-auto-confirm]').count(), 0)

    def test_generic_confirmation_uses_trusted_click(self):
        self.page.set_content('''<div role="button" tabindex="0"
            onclick="if(event.isTrusted) window.accepted=true">Accept</div>''')
        self.assertEqual(BrowserSession.auto_click_confirmations(self.page, max_clicks=1), 1)
        self.assertTrue(self.page.evaluate("window.accepted"))
        self.assertEqual(self.page.locator('[data-fb-auto-confirm]').count(), 0)

    def test_failed_native_click_does_not_fall_back_to_script_click(self):
        flow = self.make_flow("demo-password")
        self.api.show_app_secret()
        with patch("playwright.sync_api.Locator.click", side_effect=Error("test click failure")):
            self.assertEqual(flow.session.confirm_page(self.page, self.logs.append, max_clicks=1), 0)
        self.assertEqual(self.page.evaluate("window.submissions"), 0)
        self.assertEqual(self.page.locator('[data-fb-auto-confirm]').count(), 0)
        self.assertTrue(any("no successful submission recorded" in message for message in self.logs))

    def install_second_confirmation(self, reveal=True):
        self.page.evaluate("""reveal => {
            const button = document.querySelector('[data-testid="sec_ac_button"]');
            button.onclick = event => {
                if (!event.isTrusted) return;
                window.submissions++;
                document.querySelector('#password').style.visibility = 'hidden';
                document.querySelector('dialog h2').textContent = 'Confirm with password';
                button.textContent = 'Confirm';
                button.onclick = nextEvent => {
                    if (!nextEvent.isTrusted) return;
                    window.submissions++;
                    if (reveal) {
                        document.querySelector('#secret-field').value = 'abababababababababababababababab';
                        document.querySelector('dialog').close();
                    }
                };
            };
        }""", reveal)

    def test_secret_flow_handles_second_confirm_with_hidden_password(self):
        flow = self.make_flow("demo-password")
        self.install_second_confirmation()
        with patch("flow.time.sleep"):
            self.assertEqual(flow._fetch_app_secret(self.page, self.api, "123"), SECRET)
        self.assertEqual(self.page.evaluate("window.submissions"), 2)
        self.assertTrue(any("follow-up password confirmation" in message for message in self.logs))

    def test_second_confirmation_is_not_repeated_if_still_visible(self):
        flow = self.make_flow("demo-password")
        self.install_second_confirmation(reveal=False)
        with patch("flow.time.sleep"), patch("flow.time.time", side_effect=[0, 1, 2, 3, 31]):
            self.assertEqual(flow._fetch_app_secret(self.page, self.api, "123"), "")
        self.assertEqual(self.page.evaluate("window.submissions"), 2)

    def test_followup_does_not_submit_visible_password_or_unrelated_dialog(self):
        session = BrowserSession(browser_name="roxy", account="selected")
        self.api.show_app_secret()
        self.assertFalse(session.confirm_password_followup(self.page))
        self.assertEqual(self.page.evaluate("window.submissions"), 0)
        self.page.set_content('''<div role="dialog">Delete application
            <button onclick="window.unrelatedClicked=true">Confirm</button></div>''')
        self.assertFalse(session.confirm_password_followup(self.page))
        self.assertIsNone(self.page.evaluate("window.unrelatedClicked"))


if __name__ == "__main__":
    unittest.main()
