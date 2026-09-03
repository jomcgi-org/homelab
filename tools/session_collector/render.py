"""Render adapter output as capped, redacted Markdown."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .models import Block, Session
from .redact import Redactor

TOOL_INPUT_LIMIT = 1024
TOOL_RESULT_LIMIT = 2 * 1024
TEXT_LIMIT = 8 * 1024
DOCUMENT_LIMIT = 400 * 1024


@dataclass
class RenderedSession:
    markdown: str
    metadata: dict[str, object]
    redactions: dict[str, int]
    truncated: bool


def _cap_bytes(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    suffix = "\n[... capped ...]"
    room = limit - len(suffix.encode("utf-8"))
    prefix = encoded[: max(room, 0)].decode("utf-8", errors="ignore")
    return prefix + suffix, True


def _yaml(value: str | None) -> str:
    return "null" if value is None else json.dumps(value, ensure_ascii=False)


def _render_block(block: Block, redactor: Redactor) -> tuple[str, bool]:
    if block.kind == "result":
        value = redactor.tool_result(block.text)
        value, capped = _cap_bytes(value, TOOL_RESULT_LIMIT)
        return f"`result:`\n{value}", capped
    value = redactor.text(block.text)
    limit = TOOL_INPUT_LIMIT if block.kind == "tool" else TEXT_LIMIT
    value, capped = _cap_bytes(value, limit)
    if block.kind == "user":
        return f"**User:** {value}", capped
    if block.kind == "assistant":
        return f"**Assistant:** {value}", capped
    return f"`tool: {redactor.text(block.name or 'unknown')}` input\n{value}", capped


def _frontmatter(metadata: dict[str, object], redactions: int, truncated: bool) -> str:
    return "\n".join(
        [
            "---",
            f"title: {_yaml(str(metadata['title']))}",
            f"provider: {metadata['provider']}",
            f"session_id: {_yaml(str(metadata['session_id']))}",
            f"cwd: {_yaml(str(metadata['cwd']))}",
            f"repo: {_yaml(metadata['repo'] if isinstance(metadata['repo'], str) else None)}",
            f"scope: {_yaml(str(metadata['scope']))}",
            f"git_branch: {_yaml(metadata['git_branch'] if isinstance(metadata['git_branch'], str) else None)}",
            f"model: {_yaml(metadata['model'] if isinstance(metadata['model'], str) else None)}",
            f"started_at: {_yaml(str(metadata['started_at']))}",
            f"ended_at: {_yaml(str(metadata['ended_at']))}",
            f"records_total: {metadata['records_total']}",
            f"records_kept: {metadata['records_kept']}",
            f"redactions: {redactions}",
            f"truncated: {str(truncated).lower()}",
            f"collector_version: {_yaml(str(metadata['collector_version']))}",
            "---",
            "",
        ]
    )


def render(session: Session, repo: str | None, scope: str) -> RenderedSession:
    redactor = Redactor()
    metadata_capped = False

    def metadata_text(value: str | None) -> str | None:
        nonlocal metadata_capped
        if value is None:
            return None
        sanitized, capped = _cap_bytes(redactor.text(value), TEXT_LIMIT)
        metadata_capped = metadata_capped or capped
        return sanitized

    metadata: dict[str, object] = {
        "title": metadata_text(session.title),
        "provider": session.provider,
        "session_id": metadata_text(session.session_id),
        "cwd": metadata_text(session.cwd),
        "repo": metadata_text(repo),
        "scope": metadata_text(scope),
        "git_branch": metadata_text(session.git_branch),
        "model": metadata_text(session.model),
        "started_at": metadata_text(session.started_at),
        "ended_at": metadata_text(session.ended_at),
        "records_total": session.records_total,
        "records_kept": session.records_kept,
        "collector_version": session.collector_version,
    }
    rendered_turns: list[str] = []
    fragment_capped = metadata_capped
    for number, turn in enumerate(session.turns, 1):
        blocks: list[str] = []
        for block in turn.blocks:
            rendered_block, capped = _render_block(block, redactor)
            blocks.append(rendered_block)
            fragment_capped = fragment_capped or capped
        rendered_turns.append(f"## Turn {number}\n" + "\n\n".join(blocks))

    def document(turns: list[str], truncated: bool) -> str:
        body = "\n\n".join(turns) + ("\n" if turns else "")
        return _frontmatter(metadata, sum(redactor.counts.values()), truncated) + body

    result = document(rendered_turns, fragment_capped)
    middle_elided = False
    if len(result.encode("utf-8")) > DOCUMENT_LIMIT and len(rendered_turns) > 8:
        count = len(rendered_turns) - 8
        rendered_turns = (
            rendered_turns[:3]
            + [f"[... elided {count} turns ...]"]
            + rendered_turns[-5:]
        )
        middle_elided = True
        result = document(rendered_turns, True)

    truncated = fragment_capped or middle_elided
    if len(result.encode("utf-8")) > DOCUMENT_LIMIT:
        truncated = True
        header = _frontmatter(metadata, sum(redactor.counts.values()), True)
        separators = max(len(rendered_turns) - 1, 0) * 2 + 1
        budget = DOCUMENT_LIMIT - len(header.encode("utf-8")) - separators
        per_turn = max(budget // max(len(rendered_turns), 1), 64)
        rendered_turns = [_cap_bytes(turn, per_turn)[0] for turn in rendered_turns]
        result = document(rendered_turns, True)
    if truncated and "truncated: true" not in result:
        result = document(rendered_turns, True)
    return RenderedSession(result, metadata, dict(redactor.counts), truncated)
