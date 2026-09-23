from __future__ import annotations

import os


def configure_runtime_environment() -> None:
    os.environ.setdefault("NODE_NO_WARNINGS", "1")
