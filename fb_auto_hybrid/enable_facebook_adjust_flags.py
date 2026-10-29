from __future__ import annotations

import argparse
import copy
import re
import sys
from dataclasses import dataclass
from typing import Any

from adjust_service import AdjustApiError, AdjustService, FACEBOOK_PARTNER_ID
from monitor_sheet import MonitorSheet


BRAND_SPLIT_RE = re.compile(r"[\s,\uFF0C\u3001;\uFF1B]+")


@dataclass
class UpdateResult:
    brand: str
    ok: bool
    message: str


def parse_brand_values(values: list[str]) -> list[str]:
    brands: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in BRAND_SPLIT_RE.split(str(value or "")):
            brand = item.strip()
            if not brand or brand in seen:
                continue
            seen.add(brand)
            brands.append(brand)
    return brands


def read_sheet_brands() -> list[str]:
    brands: list[str] = []
    seen: set[str] = set()
    for row in MonitorSheet().read_rows():
        brand = str(row[0]).strip() if row and len(row) >= 1 and row[0] else ""
        if not brand or brand in seen:
            continue
        seen.add(brand)
        brands.append(brand)
    return brands


def partner_event_android_name(item: dict[str, Any]) -> str:
    android = item.get("android")
    if isinstance(android, dict):
        return str(android.get("name") or "").strip()
    if isinstance(android, list):
        for setting in android:
            if isinstance(setting, dict) and setting.get("key") == "name":
                return str(setting.get("value") or "").strip()
    return ""


def build_flags_payload(partner_settings: dict[str, Any]) -> tuple[dict[str, Any], dict[str, bool]]:
    settings_by_platform = partner_settings.get("partner_app_settings")
    if not isinstance(settings_by_platform, dict) or not isinstance(settings_by_platform.get("android"), dict):
        raise AdjustApiError("Adjust Facebook partner settings do not include android platform")

    partner_app_settings = copy.deepcopy(settings_by_platform)
    android_settings = partner_app_settings["android"]
    raw_settings = android_settings.pop("settings", [])
    for item in raw_settings:
        if isinstance(item, dict) and item.get("key"):
            value = item.get("value")
            if value not in (None, ""):
                android_settings[str(item["key"])] = value

    before = {
        "parameter_forwarding": bool(android_settings.get("parameter_forwarding", False)),
        "uninstall_forwarding": bool(android_settings.get("uninstall_forwarding", False)),
    }
    android_settings["integrated"] = True
    android_settings["parameter_forwarding"] = True
    android_settings["uninstall_forwarding"] = True
    android_settings["parameter_mapping"] = android_settings.get("parameter_mapping") or {}
    android_settings["parameter_sharing"] = android_settings.get("parameter_sharing") or []
    android_settings["ad_revenue_forwarding_sources"] = android_settings.get("ad_revenue_forwarding_sources") or []

    partner_event_type_settings: list[dict[str, Any]] = []
    for item in partner_settings.get("partner_event_type_settings") or []:
        if not isinstance(item, dict):
            continue
        event_id = item.get("event_type_id")
        if event_id is None:
            continue
        partner_name = partner_event_android_name(item)
        if not partner_name:
            continue
        partner_event_type_settings.append({"event_type_id": event_id, "android": {"name": partner_name}})

    return (
        {
            "id": FACEBOOK_PARTNER_ID,
            "partner_app_settings": partner_app_settings,
            "partner_event_type_settings": partner_event_type_settings,
        },
        before,
    )


def resolve_adjust_app(service: AdjustService, brand: str):
    apps = service._unique_apps(
        [
            *service.search_partner_mobile_apps(brand),
            *service.search_apps(brand, connected=True),
            *service.search_apps_plain(brand),
        ]
    )
    if not apps:
        raise AdjustApiError(f"No Adjust app found for brand {brand}")
    return service._pick_brand_match(brand, apps)


def inspect_current_flags(service: AdjustService, brand: str) -> dict[str, Any]:
    app = resolve_adjust_app(service, brand)
    settings = service.get_partner_settings(app.token)
    _, before = build_flags_payload(settings)
    return {
        "brand": brand,
        "app_token": app.token,
        "app_name": app.name,
        "before": before,
    }


def enable_facebook_forwarding_flags(service: AdjustService, brand: str) -> dict[str, Any]:
    clean_brand = str(brand).strip()
    if not clean_brand:
        raise AdjustApiError("Adjust brand is empty; cannot update Facebook forwarding flags")

    app = resolve_adjust_app(service, clean_brand)
    settings = service.get_partner_settings(app.token)
    payload, before = build_flags_payload(settings)
    resp = service._request("POST", f"apps/{app.token}/settings/partners", json=payload)
    return {
        "brand": clean_brand,
        "app_token": app.token,
        "app_name": app.name,
        "status": resp.status_code,
        "ok": resp.ok,
        "before": before,
        "after": {
            "parameter_forwarding": True,
            "uninstall_forwarding": True,
        },
    }


def update_one(service: AdjustService, brand: str, *, apply: bool, only_needed: bool) -> UpdateResult:
    try:
        current = inspect_current_flags(service, brand)
        before = current["before"]
        already_enabled = bool(before.get("parameter_forwarding")) and bool(before.get("uninstall_forwarding"))
        if already_enabled and only_needed:
            return UpdateResult(brand, True, f"skip already enabled: {current['app_name']} ({current['app_token']})")
        if not apply:
            return UpdateResult(
                brand,
                True,
                "dry-run "
                f"{current['app_name']} ({current['app_token']}) "
                f"parameter={before.get('parameter_forwarding')} uninstall={before.get('uninstall_forwarding')}",
            )

        result = enable_facebook_forwarding_flags(service, brand)
        old = result["before"]
        return UpdateResult(
            brand,
            True,
            f"updated {result['app_name']} ({result['app_token']}) "
            f"parameter {old.get('parameter_forwarding')}->True "
            f"uninstall {old.get('uninstall_forwarding')}->True",
        )
    except Exception as exc:
        return UpdateResult(brand, False, str(exc))


def interactive_main() -> int:
    print("Enable Adjust Facebook flags: parameter_forwarding=True, uninstall_forwarding=True")
    print("Input brands separated by spaces, comma, Chinese comma, ideographic comma, or new lines.")
    brands_text = input("Brands: ").strip()
    brands = parse_brand_values([brands_text])
    if not brands:
        print("No brand input. Exit.")
        return 1

    apply_text = input("Write to Adjust? input y to apply, otherwise dry-run: ").strip().lower()
    apply = apply_text in {"y", "yes", "1", "true"}

    token_text = input("Adjust API Token (empty = app_config.json): ").strip()
    service = AdjustService(token_text) if token_text else AdjustService.from_config()

    return run_updates(service, brands, apply=apply, include_enabled=apply)


def run_updates(service: AdjustService, brands: list[str], *, apply: bool, include_enabled: bool) -> int:
    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"Brands: {len(brands)}")

    ok_count = 0
    fail_count = 0
    for index, brand in enumerate(brands, start=1):
        result = update_one(service, brand, apply=apply, only_needed=not include_enabled)
        if result.ok:
            ok_count += 1
            status = "OK"
        else:
            fail_count += 1
            status = "FAIL"
        print(f"[{index}/{len(brands)}] {status} {result.brand}: {result.message}")

    print(f"Done. ok={ok_count} fail={fail_count}")
    return 0 if fail_count == 0 else 2


def main() -> int:
    if len(sys.argv) == 1:
        return interactive_main()

    parser = argparse.ArgumentParser(
        description="Enable Adjust Facebook parameter_forwarding and uninstall_forwarding for existing apps."
    )
    parser.add_argument("--apply", action="store_true", help="Actually write changes. Without this flag it only previews.")
    parser.add_argument("--brands", nargs="*", default=[], help="Optional brands. Supports spaces and commas.")
    parser.add_argument("--all", action="store_true", help="Read brands from monitor sheet.")
    parser.add_argument("--include-enabled", action="store_true", help="Submit even if both flags are already enabled.")
    parser.add_argument("--token", default="", help="Adjust API token. If empty, app_config.json is used.")
    args = parser.parse_args()

    brands = parse_brand_values(args.brands)
    if not brands and args.all:
        brands = read_sheet_brands()

    if not brands:
        print("No brands found. Use --brands sprcas783 or --all.")
        return 1

    service = AdjustService(args.token) if args.token else AdjustService.from_config()
    return run_updates(service, brands, apply=args.apply, include_enabled=args.include_enabled)


if __name__ == "__main__":
    raise SystemExit(main())
