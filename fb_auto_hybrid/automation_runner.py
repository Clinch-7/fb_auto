from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from runtime_env import configure_runtime_environment


configure_runtime_environment()

from config import DEFAULT_BROWSER
from flow import FlowContext, FlowRunError, HybridFlow
from main import enforce_expiration


RESULT_MARKER = "__FB_AUTO_RESULT__"


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


_force_utf8_stdio()


def _emit(payload: dict[str, Any]) -> None:
    print(RESULT_MARKER + json.dumps(payload, ensure_ascii=False), flush=True)


def _load_payload(path: str) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("automation payload must be a JSON object")
    return data


def _log(message: str) -> None:
    print(str(message), file=sys.stderr, flush=True)


def run_brand(payload: dict[str, Any]) -> dict[str, Any]:
    flow = HybridFlow(
        FlowContext(browser=DEFAULT_BROWSER, account=str(payload.get("roxy_profile_id") or "") or None),
        logger=_log,
    )
    fb_app_id = flow.run_brand(
        brand_name=str(payload.get("brand") or ""),
        contact_email=None,
        activity_class_name=None,
        publish=True,
        monitor_secret=None,
        sync_monitor=True,
        adjust_brand_name=str(payload.get("adjust_brand_name") or payload.get("brand") or ""),
        monitor_email=str(payload.get("roxy_profile_email") or ""),
    )
    return {"success": True, "fb_app_id": fb_app_id}


def run_brand_override(payload: dict[str, Any]) -> dict[str, Any]:
    flow = HybridFlow(
        FlowContext(browser=DEFAULT_BROWSER, account=str(payload.get("roxy_profile_id") or "") or None),
        logger=_log,
    )
    fb_app_id = flow.run_brand_override(
        brand_name=str(payload.get("brand") or ""),
        override_brand_name=str(payload.get("override_brand") or ""),
        contact_email=None,
        activity_class_name=None,
        monitor_secret=None,
        sync_monitor=True,
        adjust_brand_name=str(payload.get("adjust_brand_name") or payload.get("brand") or ""),
        monitor_email=str(payload.get("roxy_profile_email") or ""),
    )
    return {"success": True, "fb_app_id": fb_app_id}


def run_ad_auth(payload: dict[str, Any]) -> dict[str, Any]:
    ids = payload.get("ad_account_ids") or []
    if not isinstance(ids, list):
        raise ValueError("ad_account_ids must be a list")
    flow = HybridFlow(
        FlowContext(browser=DEFAULT_BROWSER, account=str(payload.get("roxy_profile_id") or "") or None),
        logger=_log,
    )
    result = flow.authorize_ad_accounts(str(payload.get("app_id") or ""), [str(item) for item in ids])
    return {
        "success": True,
        "added_ids": result.added_ids,
        "duplicate_ids": result.duplicate_ids,
        "submitted_ids": result.submitted_ids,
    }


def run_ad_remove(payload: dict[str, Any]) -> dict[str, Any]:
    ids = payload.get("ad_account_ids") or []
    if not isinstance(ids, list):
        raise ValueError("ad_account_ids must be a list")
    flow = HybridFlow(
        FlowContext(browser=DEFAULT_BROWSER, account=str(payload.get("roxy_profile_id") or "") or None),
        logger=_log,
    )
    result = flow.remove_authorized_ad_accounts(str(payload.get("app_id") or ""), [str(item) for item in ids])
    return {
        "success": True,
        "removed_ids": result.removed_ids,
        "missing_ids": result.missing_ids,
        "submitted_ids": result.submitted_ids,
    }


def run_payload(payload: dict[str, Any]) -> dict[str, Any]:
    mode = str(payload.get("mode") or "")
    if mode == "brand":
        return run_brand(payload)
    if mode == "brand_override":
        return run_brand_override(payload)
    if mode == "ad_auth":
        return run_ad_auth(payload)
    if mode == "ad_remove":
        return run_ad_remove(payload)
    raise ValueError(f"unknown automation mode: {mode}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Facebook automation in an isolated process.")
    parser.add_argument("--payload", required=True)
    return parser


def main() -> None:
    enforce_expiration()
    args = build_parser().parse_args()
    try:
        result = run_payload(_load_payload(args.payload))
        _emit(result)
    except Exception as exc:
        created_app_id = exc.created_app_id if isinstance(exc, FlowRunError) else ""
        cleanup_message = exc.cleanup_message if isinstance(exc, FlowRunError) else ""
        _emit(
            {
                "success": False,
                "error": str(exc),
                "detail": traceback.format_exc(),
                "created_app_id": created_app_id,
                "cleanup_message": cleanup_message,
            }
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
