from __future__ import annotations

import os


def flag(name: str) -> bool:
    return os.environ.get(name, "").lower() == "true"
