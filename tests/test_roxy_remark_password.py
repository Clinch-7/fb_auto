import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fb_auto_hybrid"))

from browser_session import BrowserSession
from flow import FlowContext, HybridFlow
from meta_api import UiRequired
from meta_fetch import FetchResult, MetaPageClient
from roxy_client import RoxyClient, RoxyProfile, RoxySettings


class RemarkPasswordTests(unittest.TestCase):
    def test_first_line_only(self):
        for remark, expected in [
            ("demo-password\r\nother account data", "demo-password"),
            ("demo-password\nother account data", "demo-password"),
            ("\nsecond-line-password", ""),
            ("   \nsecond-line-password", ""),
            ("", ""),
        ]:
            profile = RoxyProfile("selected", "account", remark=remark)
            self.assertEqual(profile.confirmation_password, expected)
            self.assertNotIn("demo-password", repr(profile))

    def test_list_preserves_empty_first_line(self):
        client = RoxyClient(RoxySettings(workspace_id=1))
        client._request = Mock(return_value={"code": 0, "data": {
            "total": 1, "rows": [{"dirId": "selected", "windowRemark": "\nother data"}],
        }})
        profile = client.list_profiles()[0]
        self.assertEqual(profile.confirmation_password, "")

    def session(self):
        session = BrowserSession(browser_name="roxy", account="selected")
        session._roxy_client = SimpleNamespace(find_profile=Mock(return_value=
            RoxyProfile("selected", "account", remark="demo-password\nignored")))
        return session

    def test_selected_profile_cached_and_cleared(self):
        session = self.session()
        self.assertEqual(session.confirmation_password(), "demo-password")
        self.assertEqual(session.confirmation_password(), "demo-password")
        session._roxy_client.find_profile.assert_called_once_with("selected")
        session.close()
        self.assertEqual(session._roxy_password, "")

    def test_no_default_or_wrong_profile_fallback(self):
        session = self.session()
        session._roxy_client.find_profile.return_value = RoxyProfile("other", "account", remark="wrong")
        self.assertEqual(session.confirmation_password(), "")
        session = BrowserSession(browser_name="chrome", account="selected")
        with patch("browser_session.RoxyClient") as client:
            self.assertEqual(session.confirmation_password(), "")
            client.assert_not_called()

    def test_read_errors_do_not_leak_remark(self):
        session = self.session()
        session._roxy_client.find_profile.side_effect = RuntimeError("demo-password")
        logs = []
        self.assertEqual(session.confirmation_password(logs.append), "")
        self.assertNotIn("demo-password", " ".join(logs))

    def test_fill_only_facebook_and_unique_visible_password(self):
        for url, count, should_fill in [
            ("https://developers.facebook.com/apps/123/settings/basic/", 1, True),
            ("https://www.facebook.com/login/", 1, True),
            ("https://facebook.com.example.org/", 1, False),
            ("http://facebook.com/", 1, False),
            ("https://facebook.com/", 2, False),
        ]:
            session = self.session()
            page = Mock(url=url)
            inputs = page.locator.return_value
            inputs.count.return_value = count
            inputs.first.input_value.return_value = ""
            logs = []
            with patch.object(BrowserSession, "auto_click_confirmations", return_value=1):
                session.confirm_page(page, logs.append)
            self.assertEqual(inputs.first.fill.called, should_fill)
            if should_fill:
                inputs.first.fill.assert_called_once_with("demo-password", timeout=3000)
            else:
                session._roxy_client.find_profile.assert_not_called()
            self.assertNotIn("demo-password", " ".join(logs))

    def test_confirmation_retry_for_secret(self):
        logs = []
        flow = HybridFlow(FlowContext("roxy", "selected"), logger=logs.append)
        page = Mock()
        api = Mock()
        secret = "a" * 32
        api.request_app_secret.side_effect = [UiRequired("confirmation required"),
            SimpleNamespace(text=json.dumps({"app_secret": secret}))]
        flow._open = Mock(return_value=page)
        flow._api_for = Mock(return_value=api)
        flow.session.confirmation_password = Mock(return_value="demo-password")
        self.assertEqual(flow._fetch_app_secret(page, api, "123"), secret)
        self.assertEqual(api.request_app_secret.call_count, 2)
        api.request_app_secret.assert_called_with("123", ajax_password="demo-password")
        self.assertNotIn("demo-password", " ".join(logs))
        self.assertNotIn(secret, " ".join(logs))

    def test_failed_password_request_not_repeated_or_logged(self):
        logs = []
        flow = HybridFlow(FlowContext("roxy", "selected"), logger=logs.append)
        page, api = Mock(), Mock()
        api.request_app_secret.side_effect = [UiRequired("confirmation required"), RuntimeError("demo-password")]
        flow._open = Mock(return_value=page)
        flow._api_for = Mock(return_value=api)
        flow.session.confirmation_password = Mock(return_value="demo-password")
        flow.session.confirm_page = Mock(return_value=0)
        with patch("flow.time.time", side_effect=[0, 31]):
            self.assertEqual(flow._fetch_app_secret(page, api, "123"), "")
        self.assertEqual(api.request_app_secret.call_count, 2)
        self.assertNotIn("demo-password", " ".join(logs))

    def test_wait_for_app_id_uses_profile_confirmation(self):
        flow = HybridFlow(FlowContext("roxy", "selected"))
        page = Mock(url="https://developers.facebook.com/apps/creation/")
        def confirm(*args, **kwargs):
            page.url = "https://developers.facebook.com/apps/123456789012345/settings/basic/"
        flow.session.confirm_page = Mock(side_effect=confirm)
        with patch("flow.time.time", side_effect=[0, 1]):
            self.assertEqual(flow._wait_for_app_id_from_url(page, 45), "123456789012345")
        flow.session.confirm_page.assert_called_once_with(page, None, max_clicks=1)

    def test_credential_request_logging_is_redacted(self):
        client = MetaPageClient(Mock())
        payload = {"ajax_password": "demo-password", "confirmed": "1"}
        result = FetchResult(200, True, "https://developers.facebook.com/apps/123/async/settings/app-secret/",
                             False, "application/json", 'demo-password ' + 'a' * 32)
        with tempfile.TemporaryDirectory() as folder, patch("meta_fetch.log_dir", return_value=Path(folder)):
            path = client._write_log(result.url, payload, result)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("demo-password", text)
            self.assertNotIn("a" * 32, text)
        self.assertEqual(payload["ajax_password"], "demo-password")


if __name__ == "__main__":
    unittest.main()
