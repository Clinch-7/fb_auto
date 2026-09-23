from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from browser_session import BrowserSession
from config import DEFAULT_BROWSER, project_dir


CAPTURE_DOMAINS = (
    "facebook.com",
    "developers.facebook.com",
    "business.facebook.com",
    "meta.com",
)


def is_interesting(url: str) -> bool:
    lower = url.lower()
    if not any(domain in lower for domain in CAPTURE_DOMAINS):
        return False
    noisy = (
        "/ajax/bz",
        "/logging_client_events",
        "/tr/",
        "/privacy_sandbox/",
        "/connect/ping",
        "google",
    )
    return not any(item in lower for item in noisy)


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture Meta browser requests")
    parser.add_argument("--browser", default=DEFAULT_BROWSER, choices=["roxy", "chrome", "edge"])
    parser.add_argument("--account", default=None)
    parser.add_argument("--seconds", type=int, default=900)
    parser.add_argument("--start-url", default="https://developers.facebook.com/apps/creation/")
    args = parser.parse_args()

    capture_dir = project_dir() / "captured_requests"
    capture_dir.mkdir(parents=True, exist_ok=True)
    out_path = capture_dir / f"live_capture_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"

    session = BrowserSession(browser_name=args.browser, account=args.account)
    page = session.start()
    seen_requests: dict[str, dict] = {}

    def write_event(event: dict) -> None:
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def on_request(request) -> None:
        if not is_interesting(request.url):
            return
        post_data = None
        try:
            post_data = request.post_data
        except Exception:
            pass
        event = {
            "type": "request",
            "ts": time.time(),
            "method": request.method,
            "url": request.url,
            "resource_type": request.resource_type,
            "headers": request.headers,
            "post_data": post_data,
        }
        seen_requests[request.url + str(id(request))] = event
        write_event(event)
        print(f"[REQ] {request.method} {request.url[:180]}", flush=True)

    def on_response(response) -> None:
        request = response.request
        if not is_interesting(response.url):
            return
        text_head = ""
        try:
            content_type = response.headers.get("content-type", "")
            if "json" in content_type or "text" in content_type or "html" in content_type:
                text_head = response.text()[:4000]
        except Exception as exc:
            text_head = f"<read response failed: {exc}>"
        event = {
            "type": "response",
            "ts": time.time(),
            "method": request.method,
            "url": response.url,
            "status": response.status,
            "headers": response.headers,
            "text_head": text_head,
        }
        write_event(event)
        print(f"[RES] {response.status} {request.method} {response.url[:180]}", flush=True)

    page.on("request", on_request)
    page.on("response", on_response)

    print(f"[capture] writing to: {out_path}", flush=True)
    print(f"[capture] opening: {args.start_url}", flush=True)
    page.goto(args.start_url, wait_until="domcontentloaded", timeout=60000)
    BrowserSession.wait_soft_networkidle(page)

    deadline = time.time() + args.seconds
    try:
        while time.time() < deadline:
            time.sleep(1)
    finally:
        try:
            session.save_cookies()
        except Exception:
            pass
        session.close()
        print(f"[capture] done: {out_path}", flush=True)


if __name__ == "__main__":
    main()
