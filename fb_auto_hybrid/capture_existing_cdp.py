from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from config import project_dir


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
        "/browser_reporting/",
        "/tr/",
        "google-analytics",
        "googletagmanager",
    )
    return not any(item in lower for item in noisy)


def attach_page(page, port: int, out_path: Path, attached_pages: set[int]) -> None:
    page_id = id(page)
    if page_id in attached_pages:
        return
    attached_pages.add(page_id)

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
            "port": port,
            "page_url": page.url,
            "method": request.method,
            "url": request.url,
            "resource_type": request.resource_type,
            "headers": request.headers,
            "post_data": post_data,
        }
        write_event(event)
        print(f"[REQ:{port}] {request.method} {request.url[:180]}", flush=True)

    def on_response(response) -> None:
        request = response.request
        if not is_interesting(response.url):
            return
        text_head = ""
        try:
            content_type = response.headers.get("content-type", "")
            if "json" in content_type or "text" in content_type or "html" in content_type:
                text_head = response.text()[:5000]
        except Exception as exc:
            text_head = f"<read response failed: {exc}>"
        event = {
            "type": "response",
            "ts": time.time(),
            "port": port,
            "page_url": page.url,
            "method": request.method,
            "url": response.url,
            "status": response.status,
            "headers": response.headers,
            "text_head": text_head,
        }
        write_event(event)
        print(f"[RES:{port}] {response.status} {request.method} {response.url[:180]}", flush=True)

    page.on("request", on_request)
    page.on("response", on_response)
    print(f"[attach:{port}] page={page.url}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Attach to existing Chrome CDP ports and capture Meta requests")
    parser.add_argument("--ports", nargs="+", type=int, required=True)
    parser.add_argument("--seconds", type=int, default=1800)
    args = parser.parse_args()

    capture_dir = project_dir() / "captured_requests"
    capture_dir.mkdir(parents=True, exist_ok=True)
    out_path = capture_dir / f"old_flow_capture_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    print(f"[capture] writing to: {out_path}", flush=True)

    connected = {}
    attached_pages: set[int] = set()
    deadline = time.time() + args.seconds

    with sync_playwright() as p:
        while time.time() < deadline:
            for port in args.ports:
                if port not in connected:
                    try:
                        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=1000)
                        connected[port] = browser
                        print(f"[connect] port={port}", flush=True)
                    except Exception:
                        continue

                browser = connected.get(port)
                if not browser:
                    continue
                try:
                    for context in browser.contexts:
                        context.on("page", lambda page, port=port: attach_page(page, port, out_path, attached_pages))
                        for page in context.pages:
                            attach_page(page, port, out_path, attached_pages)
                except Exception as exc:
                    print(f"[disconnect] port={port}: {exc}", flush=True)
                    connected.pop(port, None)

            time.sleep(1)

        for browser in connected.values():
            try:
                disconnect = getattr(browser, "disconnect", None)
                if callable(disconnect):
                    disconnect()
                else:
                    browser.close()
            except Exception:
                pass

    print(f"[capture] done: {out_path}", flush=True)


if __name__ == "__main__":
    main()

