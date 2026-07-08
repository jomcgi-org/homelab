"""Load the committed Elo snapshot for the 48 WC2026 teams."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_PATH = Path(__file__).parent / "ratings" / "elo_2026.json"


@lru_cache(maxsize=1)
def load_elo() -> dict[str, float]:
    data = json.loads(_PATH.read_text())["ratings"]
    return {code: float(v) for code, v in data.items()}


def elo_for(table: dict[str, float], fifa_code: str) -> float:
    return table[fifa_code]  # KeyError if a team is missing from the snapshot
