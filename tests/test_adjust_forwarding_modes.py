import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fb_auto_hybrid"))

import adjust_service
from adjust_service import AdjustService


class FacebookForwardingModeTests(unittest.TestCase):
    def setUp(self):
        self.service = AdjustService("offline-test-token")

    def tearDown(self):
        self.service.session.close()

    def settings(self, previous):
        return {
            "partner_app_settings": {
                "android": {
                    "app_id": "123",
                    "parameter_forwarding": previous,
                    "uninstall_forwarding": previous,
                    "session_forwarding": False,
                    "revenue_forwarding": True,
                    "settings": [
                        {"key": "parameter_forwarding", "value": previous},
                        {"key": "uninstall_forwarding", "value": previous},
                    ],
                },
            },
            "partner_event_type_settings": [{"event_type_id": 1, "event_type_name": "Register"}],
        }

    def test_source_defaults_to_off(self):
        self.assertIs(adjust_service.FACEBOOK_PARAMETER_AND_UNINSTALL_ENABLED, False)

    def test_both_modes_force_create_and_edit_without_mutating_input(self):
        for enabled in (True, False):
            for previous in (True, False):
                with self.subTest(enabled=enabled, previous=previous), patch.object(
                    adjust_service, "FACEBOOK_PARAMETER_AND_UNINSTALL_ENABLED", enabled
                ):
                    settings = self.settings(previous)
                    before = copy.deepcopy(settings)
                    mappings = {"Register": "fb_mobile_complete_registration"}
                    edit = self.service.build_facebook_partner_payload(settings, "456", mappings)
                    create = self.service.build_facebook_partner_link_payload("token", settings, "456", mappings)
                    for android in (edit["partner_app_settings"]["android"], create["mobile_apps"][0]["partner_app_settings"]["android"]):
                        self.assertIs(android["parameter_forwarding"], enabled)
                        self.assertIs(android["uninstall_forwarding"], enabled)
                        self.assertIs(android["session_forwarding"], False)
                        self.assertIs(android["revenue_forwarding"], True)
                        self.assertEqual(android["app_id"], "456")
                    self.assertEqual(settings, before)

    def test_facebook_switch_does_not_change_tiktok_payload(self):
        settings = self.settings(False)
        with patch.object(adjust_service, "FACEBOOK_PARAMETER_AND_UNINSTALL_ENABLED", True):
            on = self.service.build_tiktok_partner_payload(settings, "456", {"Register": "register"})
        with patch.object(adjust_service, "FACEBOOK_PARAMETER_AND_UNINSTALL_ENABLED", False):
            off = self.service.build_tiktok_partner_payload(settings, "456", {"Register": "register"})
        self.assertEqual(on, off)


if __name__ == "__main__":
    unittest.main()
