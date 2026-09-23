from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from typing import Any

from app_config import required, value


def _content_to_text(message_type: str, content: str) -> str:
    text = str(content or "")
    try:
        data = json.loads(text)
    except Exception:
        return text

    if message_type == "text" and isinstance(data, dict):
        return str(data.get("text") or "")

    if message_type == "post" and isinstance(data, dict):
        parts: list[str] = []
        post = data.get("zh_cn") or data.get("en_us") or data
        content_rows = post.get("content") if isinstance(post, dict) else None
        if isinstance(content_rows, list):
            for row in content_rows:
                if not isinstance(row, list):
                    continue
                for item in row:
                    if not isinstance(item, dict):
                        continue
                    if item.get("tag") == "at":
                        parts.append(" ")
                    elif item.get("tag") == "text":
                        parts.append(str(item.get("text") or ""))
        return "".join(parts).strip() or text

    return text


def _emit_event(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False), flush=True)


def run(seconds: int) -> None:
    try:
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
    except Exception as exc:
        raise RuntimeError("缺少 lark-oapi，请先安装或重新打包包含该依赖") from exc

    app_id = required("lark", "app_id", "lark.app_id")
    app_secret = required("lark", "app_secret", "lark.app_secret")
    domain = str(value("lark", "openapi_base", "https://open.larksuite.com") or "https://open.larksuite.com").strip()
    if not domain.startswith(("http://", "https://")):
        domain = "https://open.larksuite.com"
    domain = domain.rstrip("/")

    if seconds > 0:
        timer = threading.Timer(seconds, lambda: os._exit(0))
        timer.daemon = True
        timer.start()

    def on_message(event: P2ImMessageReceiveV1) -> None:
        payload = event.event
        if not payload or not payload.message:
            return
        message = payload.message
        sender = payload.sender
        sender_id = ""
        if sender and sender.sender_id:
            sender_id = sender.sender_id.open_id or sender.sender_id.user_id or sender.sender_id.union_id or ""

        text = _content_to_text(message.message_type or "", message.content or "")
        _emit_event(
            {
                "event_id": message.message_id or "",
                "chat_id": message.chat_id or "",
                "sender_id": sender_id,
                "content": text,
                "message_type": message.message_type or "",
                "chat_type": message.chat_type or "",
                "create_time": str(message.create_time or ""),
            }
        )

    handler = (
        lark.EventDispatcherHandler.builder("", "", lark.LogLevel.ERROR)
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    client = lark.ws.Client(app_id, app_secret, log_level=lark.LogLevel.ERROR, event_handler=handler, domain=domain)
    print("[sdk-event] ready event_key=im.message.receive_v1", file=sys.stderr, flush=True)
    client.start()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Lark SDK event source for Facebook automation.")
    parser.add_argument("--seconds", type=int, default=3600)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    run(seconds=args.seconds)


if __name__ == "__main__":
    main()
