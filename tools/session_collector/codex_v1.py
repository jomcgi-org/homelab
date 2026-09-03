"""Adapter for the current Codex rollout JSONL format."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Block, Session, Turn
from .redact import Redactor

FORMAT_VERSION = "codex-v1"
IGNORED_USER_PREFIXES = (
    "<environment_context>",
    "<recommended_plugins>",
    "<user_instructions>",
    "<turn_aborted>",
    "# AGENTS.md",
)


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
    turns: list[Turn] = []
    current: Turn | None = None
    kept = 0
    session_id = path.stem
    cwd = ""
    model: str | None = None
    first_user = ""
    event_user = ""
    branch: str | None = None
    origin: str | None = None

    for record in records:
        record_type = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record_type == "session_meta":
            kept += 1
            session_id = str(payload.get("id") or session_id)
            cwd = str(payload.get("cwd") or cwd)
            git = payload.get("git")
            if isinstance(git, dict):
                if isinstance(git.get("branch"), str):
                    branch = git["branch"]
                if isinstance(git.get("repository_url"), str):
                    origin = git["repository_url"]
            continue
        if record_type == "event_msg" and payload.get("type") == "user_message":
            message = payload.get("message")
            if isinstance(message, str) and message.strip() and not event_user:
                event_user = message.strip().splitlines()[0]
            continue
        if record_type == "turn_context":
            kept += 1
            if isinstance(payload.get("model"), str):
                model = payload["model"]
            continue
        if record_type != "response_item":
            continue
        item_type = payload.get("type")
        if item_type == "message":
            role = payload.get("role")
            content = payload.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, list):
                continue
            texts = [
                item["text"]
                for item in content
                if isinstance(item, dict)
                and item.get("type") in {"input_text", "output_text"}
                and isinstance(item.get("text"), str)
            ]
            if not texts:
                continue
            if role == "user":
                texts = [
                    text
                    for text in texts
                    if not text.lstrip().startswith(IGNORED_USER_PREFIXES)
                ]
                if not texts:
                    continue
            kept += 1
            if role == "user":
                current = Turn([Block("user", text) for text in texts])
                turns.append(current)
                if not first_user:
                    first_user = next(
                        (
                            text.strip().splitlines()[0]
                            for text in texts
                            if text.strip()
                        ),
                        "",
                    )
            elif current is not None:
                current.blocks.extend(Block("assistant", text) for text in texts)
        elif item_type in {"custom_tool_call", "function_call"}:
            kept += 1
            if current is not None:
                tool_input = payload.get("input", payload.get("arguments", ""))
                if not isinstance(tool_input, str):
                    tool_input = json.dumps(
                        tool_input, ensure_ascii=False, sort_keys=True
                    )
                current.blocks.append(
                    Block("tool", tool_input, str(payload.get("name") or "unknown"))
                )
        elif item_type in {"custom_tool_call_output", "function_call_output"}:
            kept += 1
            if current is not None:
                output = payload.get("output", "")
                if not isinstance(output, str):
                    output = json.dumps(output, ensure_ascii=False, sort_keys=True)
                current.blocks.append(Block("result", output))

    return Session(
        provider="codex",
        session_id=session_id,
        cwd=cwd,
        git_branch=branch,
        model=model,
        started_at=timestamps[0] if timestamps else "",
        ended_at=timestamps[-1] if timestamps else "",
        title=Redactor().text(event_user or first_user)[:80],
        records_total=len(records),
        records_kept=kept,
        collector_version=FORMAT_VERSION,
        turns=turns,
        git_origin=origin,
    )
