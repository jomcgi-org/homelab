"""Markdown-aware text chunking for embedding.

Storage-agnostic: returns plain `Chunk` dicts with only `index`,
`section_header`, and `text`. Callers are responsible for attaching
storage concerns like content hashes, source URLs, or titles.

This is a narrowed port of the vault_mcp chunker. Heading-aware
splitting and fenced-code-block handling are preserved verbatim.
"""

from __future__ import annotations

import re
from typing import TypedDict


class Chunk(TypedDict):
    index: int
    section_header: str
    text: str


def _estimate_tokens(text: str) -> int:
    return int(len(text.split()) * 1.3)


def _split_by_headers(content: str) -> list[tuple[str, str]]:
    pattern = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)
    sections: list[tuple[str, str]] = []
    last_pos = 0
    last_header = ""
    for match in pattern.finditer(content):
        if last_pos < match.start():
            body = content[last_pos : match.start()].strip()
            if body:
                sections.append((last_header, body))
        last_header = match.group(0).strip()
        last_pos = match.end()
    remaining = content[last_pos:].strip()
    if remaining:
        sections.append((last_header, remaining))
    if not sections:
        sections = [("", content.strip())]
    return sections


def _split_paragraphs(text: str, max_tokens: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    parts: list[str] = []
    in_code_block = False
    code_block_lines: list[str] = []
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            if in_code_block:
                code_block_lines.append(line)
                parts.append("\n".join(code_block_lines))
                code_block_lines = []
                in_code_block = False
            else:
                in_code_block = True
                code_block_lines = [line]
        elif in_code_block:
            code_block_lines.append(line)
        else:
            parts.append(line)
    if code_block_lines:
        parts.append("\n".join(code_block_lines))
    paragraphs: list[str] = []
    current_para: list[str] = []
    for part in parts:
        if part.strip().startswith("```"):
            if current_para:
                paragraphs.append("\n".join(current_para))
                current_para = []
            paragraphs.append(part)
        elif part.strip() == "":
            if current_para:
                paragraphs.append("\n".join(current_para))
                current_para = []
        else:
            current_para.append(part)
    if current_para:
        paragraphs.append("\n".join(current_para))
    for para in paragraphs:
        para_tokens = _estimate_tokens(para)
        if current_tokens + para_tokens > max_tokens and current:
            chunks.append("\n\n".join(current))
            current = []
            current_tokens = 0
        if para_tokens > max_tokens:
            # Split oversized paragraph by words
            words = para.split()
            buf: list[str] = []
            buf_tokens = 0
            for word in words:
                word_tokens = _estimate_tokens(word)
                if buf_tokens + word_tokens > max_tokens and buf:
                    if current:
                        chunks.append("\n\n".join(current))
                        current = []
                        current_tokens = 0
                    chunks.append(" ".join(buf))
                    buf = []
                    buf_tokens = 0
                buf.append(word)
                buf_tokens += word_tokens
            if buf:
                current.append(" ".join(buf))
                current_tokens += buf_tokens
        else:
            current.append(para)
            current_tokens += para_tokens
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def chunk_markdown(
    content: str,
    *,
    max_tokens: int = 512,
    min_tokens: int = 50,
) -> list[Chunk]:
    """Split a markdown document into heading-aware chunks.

    Empty / whitespace-only input returns an empty list. Otherwise the
    document is split by top-three heading levels, each section is split
    into paragraph-aware chunks no larger than ``max_tokens``, and small
    adjacent chunks sharing a heading are merged up to ``min_tokens``.

    Returns a list of :class:`Chunk` dicts with dense, zero-based ``index``.
    """
    if not content.strip():
        return []
    sections = _split_by_headers(content)
    raw_chunks: list[tuple[str, str]] = []
    for header, body in sections:
        sub_chunks = _split_paragraphs(body, max_tokens)
        for chunk in sub_chunks:
            raw_chunks.append((header, chunk))
    merged: list[tuple[str, str]] = []
    for header, chunk_text in raw_chunks:
        if (
            merged
            and merged[-1][0] == header
            and _estimate_tokens(chunk_text) < min_tokens
            and _estimate_tokens(merged[-1][1] + "\n\n" + chunk_text) <= max_tokens
        ):
            prev_header, prev_text = merged[-1]
            merged[-1] = (prev_header, prev_text + "\n\n" + chunk_text)
        else:
            merged.append((header, chunk_text))
    return [
        Chunk(index=idx, section_header=header, text=text)
        for idx, (header, text) in enumerate(merged)
    ]
