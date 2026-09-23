from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import Page

from config import log_dir


@dataclass
class FetchResult:
    status: int
    ok: bool
    url: str
    redirected: bool
    content_type: str
    text: str

    def json(self) -> Any:
        return json.loads(self.text)


class MetaPageClient:
    def __init__(self, page: Page) -> None:
        self.page = page

    def extract_tokens(self) -> dict[str, str]:
        return self.page.evaluate(
            """
            () => {
                const out = {};
                const sources = [
                    window.requireLazy,
                    window.__bbox,
                    window.Env,
                    window.SiteData,
                    window.DTSGInitialData,
                    window.LSD,
                    window.CurrentUserInitialData,
                    window.__SERVER_CONTEXT__,
                ];

                function walk(value, depth = 0) {
                    if (!value || depth > 5) return;
                    if (typeof value !== 'object') return;
                    for (const [k, v] of Object.entries(value)) {
                        if (typeof v === 'string') {
                            const key = k.toLowerCase();
                            if (['fb_dtsg', 'token', 'lsd', 'user_id', 'accountid', 'revision', '__spin_r'].includes(key)) {
                                out[k] = v;
                            }
                        } else if (typeof v === 'number') {
                            const key = k.toLowerCase();
                            if (['user_id', 'accountid', 'revision', '__spin_r'].includes(key)) {
                                out[k] = String(v);
                            }
                        } else {
                            walk(v, depth + 1);
                        }
                    }
                }

                for (const source of sources) walk(source);

                for (const input of document.querySelectorAll('input[name]')) {
                    const name = input.getAttribute('name');
                    if (name && ['fb_dtsg', 'lsd', 'jazoest', '__user', '__a', '__req', '__hs', '__rev', '__s', '__hsi', '__dyn', 'dpr'].includes(name)) {
                        out[name] = input.value || '';
                    }
                }

                const html = document.documentElement.innerHTML;
                const patterns = {
                    fb_dtsg: /["']DTSGInitialData["'][\\s\\S]{0,500}?["']token["']\\s*:\\s*["']([^"']+)["']/,
                    lsd: /["']LSD["'][\\s\\S]{0,500}?["']token["']\\s*:\\s*["']([^"']+)["']/,
                    __rev: /["']__spin_r["']\\s*,\\s*["']?(\\d+)["']?/,
                    __hs: /["']haste_session["']\\s*:\\s*["']([^"']+)["']/,
                    __hsi: /["']hsi["']\\s*:\\s*["']([^"']+)["']/,
                };
                for (const [key, pattern] of Object.entries(patterns)) {
                    if (!out[key]) {
                        const match = html.match(pattern);
                        if (match) out[key] = match[1];
                    }
                }

                if (!out.__user) {
                    const userMatch = html.match(/"USER_ID"\\s*:\\s*"?(\\d+)"?/)
                        || html.match(/"actorID"\\s*:\\s*"?(\\d+)"?/)
                        || html.match(/"accountID"\\s*:\\s*"?(\\d+)"?/);
                    if (userMatch) out.__user = userMatch[1];
                }

                if (!out.jazoest && out.fb_dtsg) {
                    let sum = 0;
                    for (const ch of out.fb_dtsg) sum += ch.charCodeAt(0);
                    out.jazoest = '2' + String(sum);
                }

                if (!out.dpr) out.dpr = String(window.devicePixelRatio || 1);
                if (!out.__a) out.__a = '1';
                if (!out.__ccg) out.__ccg = 'UNKNOWN';

                return out;
            }
            """
        )

    def fetch_form(self, url: str, payload: dict[str, Any], method: str = "POST") -> FetchResult:
        result = self.page.evaluate(
            """
            async ({url, payload, method}) => {
                const body = new URLSearchParams();
                for (const [key, value] of Object.entries(payload || {})) {
                    if (value === undefined || value === null) continue;
                    body.append(key, String(value));
                }
                const res = await fetch(url, {
                    method,
                    credentials: 'include',
                    headers: {
                        'content-type': 'application/x-www-form-urlencoded',
                        'accept': '*/*',
                    },
                    body: method === 'GET' ? undefined : body.toString(),
                });
                return {
                    status: res.status,
                    ok: res.ok,
                    url: res.url,
                    redirected: res.redirected,
                    content_type: res.headers.get('content-type') || '',
                    text: await res.text(),
                };
            }
            """,
            {"url": url, "payload": payload, "method": method},
        )
        fetch_result = FetchResult(**result)
        self._write_log(url, payload, fetch_result)
        return fetch_result

    def fetch_json(self, url: str, payload: dict[str, Any], method: str = "POST") -> FetchResult:
        result = self.page.evaluate(
            """
            async ({url, payload, method}) => {
                const res = await fetch(url, {
                    method,
                    credentials: 'include',
                    headers: {
                        'content-type': 'application/json',
                        'accept': 'application/json, text/plain, */*',
                    },
                    body: method === 'GET' ? undefined : JSON.stringify(payload || {}),
                });
                return {
                    status: res.status,
                    ok: res.ok,
                    url: res.url,
                    redirected: res.redirected,
                    content_type: res.headers.get('content-type') || '',
                    text: await res.text(),
                };
            }
            """,
            {"url": url, "payload": payload, "method": method},
        )
        fetch_result = FetchResult(**result)
        self._write_log(url, payload, fetch_result)
        return fetch_result

    def _write_log(self, url: str, payload: dict[str, Any], result: FetchResult) -> Path:
        path = log_dir() / f"meta_fetch_{int(time.time() * 1000)}.json"
        with path.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "url": url,
                    "payload": payload,
                    "result": {
                        "status": result.status,
                        "ok": result.ok,
                        "url": result.url,
                        "redirected": result.redirected,
                        "content_type": result.content_type,
                        "text_head": result.text[:12000],
                    },
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        return path
