from __future__ import annotations

import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


ENDPOINT = "http://127.0.0.1:10768"
KEYWORDS = ["ad", "account", "advertis", "advanced", "settings", "save", "authorized"]


def main() -> None:
    out = Path("runtime") / f"ad_auth_capture_{int(time.time())}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(ENDPOINT)
        context = browser.contexts[0]
        pages = [page for page in context.pages if not page.is_closed()]
        page = pages[-1] if pages else context.new_page()
        print(f"[capture] attached page={page.url}", flush=True)
        print(f"[capture] writing {out.resolve()}", flush=True)

        def interested(url: str, post_data: str | None = None) -> bool:
            text = ((url or "") + " " + (post_data or "")).lower()
            return any(keyword in text for keyword in KEYWORDS)

        pending = {}

        def on_request(request):
            try:
                post = request.post_data or ""
                if request.method in ("POST", "PUT", "PATCH") and interested(request.url, post):
                    pending[id(request)] = {
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "method": request.method,
                        "url": request.url,
                        "post_data": post,
                        "headers": {
                            key: value
                            for key, value in request.headers.items()
                            if key.lower() in {"content-type", "x-fb-lsd", "x-asbd-id", "referer"}
                        },
                    }
                    print(f"[request] {request.method} {request.url}", flush=True)
            except Exception as exc:
                print(f"[request-error] {exc}", flush=True)

        def on_response(response):
            try:
                request = response.request
                row = pending.pop(id(request), None)
                if not row:
                    return
                try:
                    text = response.text(timeout=5000)
                except Exception as exc:
                    text = f"<response text failed: {exc}>"
                row["response"] = {
                    "status": response.status,
                    "url": response.url,
                    "headers": {"content-type": response.headers.get("content-type", "")},
                    "text_head": text[:6000],
                }
                with out.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(f"[response] {response.status} {response.url}", flush=True)
            except Exception as exc:
                print(f"[response-error] {exc}", flush=True)

        page.on("request", on_request)
        page.on("response", on_response)
        print("[capture] ready. Fill and save in browser. Waiting 180s...", flush=True)
        page.wait_for_timeout(180000)
        browser.disconnect()
        print("[capture] done", flush=True)


if __name__ == "__main__":
    main()
