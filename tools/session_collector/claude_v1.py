"""Adapter for the current Claude Code JSONL format."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Block, Session, Turn
from .redact import Redactor

FORMAT_VERSION = "claude-v1"
IGNORED_USER_PREFIXES = (
    "<command-name>",
    "<local-command-stdout>",
    "[Request interrupted",
)
TASK_NOTIFICATION = "<task-notification>"
BASH_STDOUT = "<bash-stdout>"


def _texts(content: object) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        item["text"]
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    ]


def _tool_result(item: dict[str, Any]) -> str:
    content = item.get("content", "")
    return "\n".join(_texts(content)) if not isinstance(content, str) else content


def parse(path: Path) -> Session:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)

    timestamps = [
        r["timestamp"] for r in records if isinstance(r.get("timestamp"), str)
    ]
    title_records = [
        r.get("aiTitle")
        for r in records
        if r.get("type") == "ai-title"
        and r.get("isSidechain") is not True
        and r.get("isMeta") is not True
        and isinstance(r.get("aiTitle"), str)
    ]
    turns: list[Turn] = []
    current: Turn | None = None
    kept = 0
    first_user = ""
    session_id = path.stem
    cwd = ""
    branch: str | None = None
    model: str | None = None

    for record in records:
        if record.get("isSidechain") is True or record.get("isMeta") is True:
            continue
        record_type = record.get("type")
        if record_type not in {"user", "assistant"}:
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        kept += 1
        session_id = str(record.get("sessionId") or session_id)
        cwd = str(record.get("cwd") or cwd)
        if isinstance(record.get("gitBranch"), str):
            branch = record["gitBranch"]

        content = message.get("content")
        if record_type == "user":
            raw_texts = _texts(content)
            user_texts = [
                text
                for text in raw_texts
                if not text.startswith(IGNORED_USER_PREFIXES)
                and not text.startswith((TASK_NOTIFICATION, BASH_STDOUT))
            ]
            if user_texts:
                current = Turn()
                turns.append(current)
                for text in user_texts:
                    current.blocks.append(Block("user", text))
                    if not first_user and text.strip():
                        first_user = text.strip().splitlines()[0]
            if isinstance(content, list) and current is not None:
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        current.blocks.append(Block("result", _tool_result(item)))
            if current is not None:
                if any(text.startswith(TASK_NOTIFICATION) for text in raw_texts):
                    current.blocks.append(Block("result", "[task notification]"))
                for text in raw_texts:
                    if text.startswith(BASH_STDOUT):
                        value = text.removeprefix(BASH_STDOUT).removesuffix(
                            "</bash-stdout>"
                        )
                        current.blocks.append(Block("result", value))
        elif isinstance(content, list):
            if isinstance(message.get("model"), str):
                model = message["model"]
            if current is None:
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    current.blocks.append(Block("assistant", item["text"]))
                elif item.get("type") == "tool_use":
                    value = item.get("input", {})
                    current.blocks.append(
                        Block(
                            "tool",
                            json.dumps(value, ensure_ascii=False, sort_keys=True),
                            str(item.get("name") or "unknown"),
                        )
                    )

    title_value = title_records[-1] if title_records else first_user
    title = Redactor().text(title_value)[:80]
    return Session(
        provider="claude",
        session_id=session_id,
        cwd=cwd,
        git_branch=branch,
        model=model,
        started_at=timestamps[0] if timestamps else "",
        ended_at=timestamps[-1] if timestamps else "",
        title=title,
        records_total=len(records),
        records_kept=kept,
        collector_version=FORMAT_VERSION,
        turns=turns,
    )
