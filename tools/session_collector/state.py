"""JSON state file helpers."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

State = dict[str, dict[str, Any]]


def load(path: Path) -> State:
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def save(path: Path, value: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def eligible(
    path: Path,
    entry: dict[str, Any] | None,
    quiet_minutes: float,
    now: float | None = None,
) -> bool:
    stat = path.stat()
    if stat.st_mtime > (time.time() if now is None else now) - quiet_minutes * 60:
        return False
    if not entry:
        return True
    if entry.get("status") in {"uploaded", "skipped"}:
        return stat.st_size > int(entry.get("size", -1))
    return True


def forget(path: Path, transcript: Path) -> bool:
    value = load(path)
    key = str(transcript.expanduser().resolve())
    existed = key in value
    value.pop(key, None)
    if existed:
        save(path, value)
    return existed
