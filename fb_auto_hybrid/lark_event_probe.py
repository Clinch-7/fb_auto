from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path


def pump(stream, out_file: Path, prefix: str) -> None:
    with out_file.open("a", encoding="utf-8", errors="replace") as f:
        for line in iter(stream.readline, ""):
            f.write(f"{prefix}{line}")
            f.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Lark message receive events.")
    parser.add_argument("--seconds", type=int, default=90)
    parser.add_argument("--out", default="runtime/lark_event_probe.log")
    args = parser.parse_args()

    out_file = Path(args.out)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text("", encoding="utf-8")

    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        r"C:\nvm4w\nodejs\lark-cli.ps1",
        "event",
        "consume",
        "im.message.receive_v1",
        "--max-events",
        "1",
        "--timeout",
        f"{args.seconds}s",
        "--as",
        "bot",
    ]

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    threads = [
        threading.Thread(target=pump, args=(process.stdout, out_file, "STDOUT "), daemon=True),
        threading.Thread(target=pump, args=(process.stderr, out_file, "STDERR "), daemon=True),
    ]
    for thread in threads:
        thread.start()

    deadline = time.time() + args.seconds + 10
    try:
        while time.time() < deadline and process.poll() is None:
            time.sleep(0.5)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    with out_file.open("a", encoding="utf-8", errors="replace") as f:
        f.write(f"EXIT {process.returncode}\n")

    return process.returncode or 0


if __name__ == "__main__":
    sys.exit(main())
