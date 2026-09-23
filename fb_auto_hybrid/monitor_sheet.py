from __future__ import annotations

import re
import time

import requests

from app_config import required, value

_lark_token_cache = {"token": "", "expire_at": 0.0}
_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)


def _lark_app_id() -> str:
    return str(value("monitor_sheet", "app_id") or value("lark", "app_id") or "").strip()


def _lark_app_secret() -> str:
    return str(value("monitor_sheet", "app_secret") or value("lark", "app_secret") or "").strip()


def _openapi_base() -> str:
    base = str(
        value("monitor_sheet", "openapi_base")
        or value("lark", "openapi_base", "https://open.larksuite.com")
        or "https://open.larksuite.com"
    ).strip()
    if not base.startswith(("http://", "https://")):
        base = "https://open.larksuite.com"
    return base.rstrip("/")


def _spreadsheet_token() -> str:
    return required("monitor_sheet", "spreadsheet_token", "monitor_sheet.spreadsheet_token")


def _sheet_id() -> str:
    return required("monitor_sheet", "sheet_id", "monitor_sheet.sheet_id")


def get_lark_tenant_token() -> str:
    now = time.time()
    if _lark_token_cache["token"] and now < _lark_token_cache["expire_at"] - 60:
        return str(_lark_token_cache["token"])

    resp = requests.post(
        f"{_openapi_base()}/open-apis/auth/v3/tenant_access_token/internal",
        headers={"Content-Type": "application/json"},
        json={
            "app_id": _lark_app_id() or required("lark", "app_id", "lark.app_id"),
            "app_secret": _lark_app_secret() or required("lark", "app_secret", "lark.app_secret"),
        },
        timeout=15,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取 Lark Token 失败: {data}")

    token = data.get("tenant_access_token", "")
    _lark_token_cache["token"] = token
    _lark_token_cache["expire_at"] = now + int(data.get("expire", 7200))
    return token


def monitor_name_for_brand(brand_name: str) -> str:
    return str(brand_name or "").strip()


class MonitorSheet:
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {get_lark_tenant_token()}", "Content-Type": "application/json"}

    def read_rows(self) -> list[list]:
        spreadsheet_token = _spreadsheet_token()
        sheet_id = _sheet_id()
        resp = requests.get(
            f"{_openapi_base()}/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{sheet_id}!A2:I1000",
            headers={"Authorization": f"Bearer {get_lark_tenant_token()}"},
            timeout=15,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"读取监控表失败: {data}")
        return data.get("data", {}).get("valueRange", {}).get("values", [])

    def write_range(self, cell_range: str, values: list[list[str]]) -> None:
        spreadsheet_token = _spreadsheet_token()
        sheet_id = _sheet_id()
        payload = {
            "valueRange": {
                "range": f"{sheet_id}!{cell_range}",
                "values": values,
            }
        }
        resp = requests.put(
            f"{_openapi_base()}/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values",
            headers=self.headers(),
            json=payload,
            timeout=15,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"write monitor sheet failed range={cell_range}: {data}")

    def write_row(self, row_num: int, values: list[str]) -> None:
        self.write_range(f"A{row_num}:C{row_num}", [values[:3]])
        if len(values) >= 4:
            self.write_range(f"I{row_num}:I{row_num}", [[values[3]]])

    def write_online_status(self, row_num: int, online_status: str) -> None:
        self.write_range(f"D{row_num}:D{row_num}", [[online_status]])

    def clear_duplicate_brand_rows(self, brand_name: str, keep_row: int | None) -> int:
        if not keep_row:
            return 0
        monitor_name = monitor_name_for_brand(brand_name)
        cleared = 0
        for index, row in enumerate(self.read_rows(), start=2):
            if index == keep_row:
                continue
            app_name = str(row[0]).strip() if len(row) >= 1 and row[0] else ""
            if app_name != monitor_name:
                continue
            self.write_range(f"A{index}:I{index}", [[""] * 9])
            cleared += 1
        return cleared

    def find_brand(self, brand_name: str) -> dict[str, str | int] | None:
        monitor_name = monitor_name_for_brand(brand_name)
        for index, row in enumerate(self.read_rows(), start=2):
            app_name = str(row[0]).strip() if len(row) >= 1 and row[0] else ""
            if app_name != monitor_name:
                continue
            return {
                "row": index,
                "name": app_name,
                "app_id": str(row[1]).strip() if len(row) >= 2 and row[1] else "",
                "app_secret": str(row[2]).strip() if len(row) >= 3 and row[2] else "",
                "online_status": str(row[3]).strip() if len(row) >= 4 and row[3] else "",
                "email": self._row_email(row),
            }
        return None

    @staticmethod
    def is_online_status(value: object) -> bool:
        text = str(value or "").strip().lower()
        return text in {"在线", "online", "正常", "active"} or "在线" in text

    @staticmethod
    def _row_email(row: list) -> str:
        if len(row) >= 9 and row[8]:
            return MonitorSheet._extract_email(row[8])
        if len(row) >= 4 and row[3] and "@" in str(row[3]):
            return MonitorSheet._extract_email(row[3])
        return ""

    @staticmethod
    def _extract_email(cell: object) -> str:
        if cell is None:
            return ""
        if isinstance(cell, list):
            for item in cell:
                email = MonitorSheet._extract_email(item)
                if email:
                    return email
            return ""
        if isinstance(cell, dict):
            for key in ("text", "link"):
                email = MonitorSheet._extract_email(cell.get(key))
                if email:
                    return email
            return ""
        match = _EMAIL_RE.search(str(cell))
        if match:
            return match.group(0).strip()
        return ""

    def sync_brand(
        self,
        brand_name: str,
        fb_app_id: str,
        fb_app_secret: str | None = None,
        roxy_email: str | None = None,
        online_status: str | None = None,
        target_row: int | None = None,
    ) -> None:
        monitor_name = monitor_name_for_brand(brand_name)
        rows = self.read_rows()
        row_num = target_row
        matched_row = None
        first_empty_row = None

        for index, row in enumerate(rows, start=2):
            app_name = str(row[0]).strip() if len(row) >= 1 and row[0] else ""
            if not app_name and first_empty_row is None:
                first_empty_row = index
            if row_num is not None and index == row_num:
                matched_row = row
            if row_num is None and app_name == monitor_name:
                row_num = index
                matched_row = row
                break

        if row_num is None:
            row_num = first_empty_row or (len(rows) + 2)

        secret_to_write = fb_app_secret or ""
        if not secret_to_write and matched_row and len(matched_row) >= 3 and matched_row[2]:
            secret_to_write = str(matched_row[2]).strip()
        email_to_write = roxy_email or ""
        if not email_to_write and matched_row:
            email_to_write = self._row_email(matched_row)

        self.write_row(row_num, [monitor_name, fb_app_id or "", secret_to_write, email_to_write])
        if online_status is not None:
            self.write_online_status(row_num, online_status)
