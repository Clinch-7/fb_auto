from __future__ import annotations

import argparse

from config import DEFAULT_BROWSER
from flow import FlowContext, HybridFlow


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete a Meta app using the current browser session.")
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--browser", default=DEFAULT_BROWSER, choices=["roxy", "chrome", "edge"])
    parser.add_argument("--account", default=None)
    args = parser.parse_args()

    flow = HybridFlow(FlowContext(browser=args.browser, account=args.account))
    page = flow.session.start()
    try:
        if not flow.session.ensure_login(flow.log, flow.manual_confirm):
            raise RuntimeError("Meta is not logged in.")
        print(flow._cleanup_failed_app(page, args.app_id))
    finally:
        flow.session.close()


if __name__ == "__main__":
    main()
