import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fb_auto_hybrid"))

from playwright.sync_api import Error, sync_playwright
from flow import FlowContext, FlowRunError, HybridFlow


HTML = """
<label for="app-name">App name</label><input id="app-name" name="basic_name">
<span id="email-label">App contact email</span><input id="contact" aria-labelledby="email-label" type="text">
<div role="button" id="next" aria-disabled="true">Next</div>
<script>
function validate() {
    document.querySelector('#next').setAttribute('aria-disabled', String(!(
        document.querySelector('#app-name').value && document.querySelector('#contact').value.includes('@')
    )));
}
document.querySelectorAll('input').forEach(el => el.addEventListener('input', validate));
function showIntro() {
    const modal = document.createElement('div');
    modal.setAttribute('role', 'dialog');
    modal.innerHTML = '<h2>There is a new way to create apps with Meta</h2><button>Create app</button>';
    modal.querySelector('button').onclick = () => {
        document.querySelector('#app-name').value = '';
        document.querySelector('#contact').value = '';
        validate();
        modal.remove();
    };
    document.body.appendChild(modal);
}
</script>
"""


class CreationDetailsTests(unittest.TestCase):
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
        self.page = self.context.new_page()
        self.page.set_content(HTML)
        self.logs = []
        self.flow = HybridFlow(FlowContext("chrome", None), logger=self.logs.append)

    def tearDown(self):
        self.context.close()

    def check_details(self):
        self.assertEqual(self.page.get_by_label('App name').input_value(), 'qstand346')
        self.assertEqual(self.page.get_by_label('App contact email').input_value(), 'test@example.com')
        self.assertTrue(self.page.get_by_role('button', name='Next', exact=True).is_enabled())

    def test_initial_modal_is_closed_before_filling(self):
        self.page.evaluate('showIntro()')
        with patch('flow.BrowserSession.wait_soft_networkidle'), patch('flow.time.sleep'):
            self.flow._prepare_creation_details(self.page, 'qstand346', 'test@example.com')
        self.check_details()
        self.assertIn('first-time create-app modal detected', self.logs[0])

    def test_late_modal_resets_form_and_details_are_refilled(self):
        self.page.evaluate("document.querySelector('#contact').addEventListener('blur', showIntro, {once:true})")
        with patch('flow.BrowserSession.wait_soft_networkidle'), patch('flow.time.sleep'):
            self.flow._prepare_creation_details(self.page, 'qstand346', 'test@example.com')
        self.check_details()
        self.assertTrue(any('refilling App details' in line for line in self.logs))

    def test_existing_form_and_labelled_text_email_work(self):
        self.flow._dismiss_new_create_apps_modal = Mock(return_value=False)
        self.flow._prepare_creation_details(self.page, 'qstand346', 'test@example.com')
        self.check_details()

    def test_account_email_overwrites_different_prefilled_email(self):
        self.page.get_by_label('App contact email').fill('old@example.com')
        self.flow._dismiss_new_create_apps_modal = Mock(return_value=False)
        self.flow._prepare_creation_details(self.page, 'qstand346', 'test@example.com')
        self.check_details()

    def test_missing_or_empty_email_does_not_fail_silently(self):
        self.flow._dismiss_new_create_apps_modal = Mock(return_value=False)
        with self.assertRaisesRegex(RuntimeError, 'email is empty'):
            self.flow._prepare_creation_details(self.page, 'qstand346', '')
        self.page.locator('#contact').evaluate('el => el.remove()')
        with self.assertRaisesRegex(RuntimeError, 'email field was not found'):
            self.flow._prepare_creation_details(self.page, 'qstand346', 'test@example.com')


class CreationEmailSourceTests(unittest.TestCase):
    def test_selected_account_email_does_not_depend_on_meta_prefill(self):
        flow = HybridFlow(FlowContext('roxy', 'selected-profile'), logger=lambda message: None)
        page, api = Mock(), Mock()
        flow.session.start = Mock(return_value=page)
        flow.session.ensure_login = Mock(return_value=True)
        flow.session.close = Mock()
        flow._api_for = Mock(return_value=api)
        flow._create_app_interface_first = Mock(side_effect=RuntimeError('stop before creating a real app'))
        vest = Mock()
        vest.get_brand_app.return_value = SimpleNamespace(
            package_name='com.example.test', fb_key_hash='test-hash',
            remarks_url='https://example.com/privacy', deploy_key_hash='com.example.MainActivity',
        )
        with patch('flow.VestService', return_value=vest), self.assertRaises(FlowRunError):
            flow.run_brand('qstand346', monitor_email='selected@example.com', sync_monitor=False)
        flow._create_app_interface_first.assert_called_once_with(page, api, 'qstand346', 'selected@example.com')
        api.default_contact_email.assert_not_called()


if __name__ == '__main__':
    unittest.main()
