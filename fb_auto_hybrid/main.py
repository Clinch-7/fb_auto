from __future__ import annotations

import argparse
import sys
from datetime import date

from config import DEFAULT_BROWSER
from flow import FlowContext, HybridFlow


EXPIRES_AFTER = date(2027, 8, 17)
EXPIRED_MESSAGE = "程序已不可用，请联系开发者。"


class ExpiredError(RuntimeError):
    pass


def is_expired(today: date | None = None) -> bool:
    return (today or date.today()) > EXPIRES_AFTER


def assert_not_expired() -> None:
    if is_expired():
        raise ExpiredError(EXPIRED_MESSAGE)


def enforce_expiration() -> None:
    if is_expired():
        print(EXPIRED_MESSAGE)
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Facebook hybrid automation")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Check Meta login and page tokens")
    status.add_argument("--browser", default=DEFAULT_BROWSER, choices=["roxy", "chrome", "edge"])
    status.add_argument("--account", default=None)

    run = subparsers.add_parser("run-brand", help="Run full brand flow")
    run.add_argument("--browser", default=DEFAULT_BROWSER, choices=["roxy", "chrome", "edge"])
    run.add_argument("--account", default=None)
    run.add_argument("--brand", required=True)
    run.add_argument("--no-publish", action="store_true")
    run.add_argument("--no-monitor", action="store_true")

    android = subparsers.add_parser("android-config", help="Configure an existing Meta app")
    android.add_argument("--browser", default=DEFAULT_BROWSER, choices=["roxy", "chrome", "edge"])
    android.add_argument("--account", default=None)
    android.add_argument("--app-id", required=True)
    android.add_argument("--app-name", required=True)
    android.add_argument("--email", required=True)
    android.add_argument("--url", required=True)
    android.add_argument("--package", required=True, dest="package_name")
    android.add_argument("--activity", required=True)
    android.add_argument("--key-hash", required=True)
    android.add_argument("--publish", action="store_true")

    return parser


def main() -> None:
    enforce_expiration()
    args = build_parser().parse_args()

    if args.command == "status":
        flow = HybridFlow(FlowContext(browser=args.browser, account=args.account))
        flow.status()
        return

    if args.command == "run-brand":
        flow = HybridFlow(FlowContext(browser=args.browser, account=args.account))
        flow.run_brand(
            brand_name=args.brand,
            contact_email=None,
            activity_class_name=None,
            publish=not args.no_publish,
            monitor_secret=None,
            sync_monitor=not args.no_monitor,
        )
        return

    if args.command == "android-config":
        flow = HybridFlow(FlowContext(browser=args.browser, account=args.account))
        flow.configure_existing_app(
            app_id=args.app_id,
            app_name=args.app_name,
            contact_email=args.email,
            url=args.url,
            package_name=args.package_name,
            activity_class_name=args.activity,
            key_hash=args.key_hash,
            publish=args.publish,
        )
        return

    raise ValueError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
