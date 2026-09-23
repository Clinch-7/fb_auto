from __future__ import annotations

from runtime_env import configure_runtime_environment


configure_runtime_environment()

from flow import FlowContext, HybridFlow
from main import enforce_expiration
from config import DEFAULT_BROWSER


# Temporary code-level debugging entry. Daily use should run gui_run.py.
MODE = "status"  # "status", "run-brand", or "android-config"

BROWSER = DEFAULT_BROWSER
ACCOUNT = ""
BRAND_NAME = ""
PUBLISH = True
SYNC_MONITOR_SHEET = True

EXISTING_APP_ID = ""
EXISTING_APP_NAME = ""
EXISTING_CONTACT_EMAIL = ""
EXISTING_PRIVACY_URL = ""
EXISTING_PACKAGE_NAME = ""
EXISTING_ACTIVITY_CLASS = ""
EXISTING_KEY_HASH = ""


def main() -> None:
    enforce_expiration()
    flow = HybridFlow(FlowContext(browser=BROWSER, account=ACCOUNT or None))

    if MODE == "status":
        flow.status()
        return

    if MODE == "run-brand":
        if not BRAND_NAME:
            raise RuntimeError("BRAND_NAME is required for run-brand mode.")
        flow.run_brand(
            brand_name=BRAND_NAME,
            contact_email=None,
            activity_class_name=None,
            publish=PUBLISH,
            monitor_secret=None,
            sync_monitor=SYNC_MONITOR_SHEET,
        )
        return

    if MODE == "android-config":
        flow.configure_existing_app(
            app_id=EXISTING_APP_ID,
            app_name=EXISTING_APP_NAME,
            contact_email=EXISTING_CONTACT_EMAIL,
            url=EXISTING_PRIVACY_URL,
            package_name=EXISTING_PACKAGE_NAME,
            activity_class_name=EXISTING_ACTIVITY_CLASS,
            key_hash=EXISTING_KEY_HASH,
            publish=PUBLISH,
        )
        return

    raise ValueError(f"Unknown MODE: {MODE}")


if __name__ == "__main__":
    main()
