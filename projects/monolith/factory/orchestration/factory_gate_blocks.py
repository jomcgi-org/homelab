"""Conservative boundaries for generated issue contracts, without receipt I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise

_TASK = r"[A-Za-z0-9_-]+"
_MARKER = re.compile(
    rf"^<!-- (?P<close>/)?factory-gate:(?P<task>{_TASK})"
    r"(?::(?P<hash>[0-9a-f]{20}))? -->$",
    re.MULTILINE,
)
_AUDIT = re.compile(r"<!-- factory-gate-hash:([0-9a-f]{20}) -->")
_SUFFIX = (
    ". Deliver repository changes default-off or staged. Operational acceptance "
    "remains on the issue; do not close it."
)


@dataclass(frozen=True)
class Block:
    start: int
    end: int
    task: str
    identity: str
    text: str


def render(task: str, identity: str, text: str) -> str:
    if not re.fullmatch(_TASK, task) or not re.fullmatch(r"[0-9a-f]{20}", identity):
        raise ValueError("invalid generated gate identity")
    if not _contract(text.split("\n")) or "<!--" in text:
        raise ValueError("unrecognized generated gate contract")
    return (
        f"<!-- factory-gate:{task} -->\n"
        f"<!-- factory-gate-hash:{identity} -->\n{text}\n"
        f"<!-- /factory-gate:{task} -->"
    )


def _contract(lines: list[str]) -> bool:
    return (
        len(lines) >= 2
        and lines[0].startswith("Conductor rescope: ")
        and lines[0].endswith(_SUFFIX)
        and all(re.fullmatch(r"- \[ \] \S.*", line) for line in lines[1:])
    )


def _visible_body(body: str) -> str:
    # Inline marker examples are ordinary prose. Mask fenced examples while
    # retaining offsets, so the issue describing this fix can itself migrate.
    visible = []
    fence = None
    for line in body.splitlines(keepends=True):
        delimiter = re.fullmatch(r" {0,3}(`{3,}|~{3,})([^\n]*)\n?", line)
        was_fenced = fence is not None
        if delimiter:
            run, rest = delimiter.groups()
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not rest.strip():
                fence = None
        visible.append(re.sub(r"[^\n]", " ", line) if was_fenced or fence else line)
    return "".join(visible)


def unowned_rescopes(body: str, found: list[Block]) -> bool:
    return any(
        not any(block.start <= match.start() < block.end for block in found)
        for match in re.finditer(r"(?m)^Conductor rescope:", _visible_body(body))
    )


def blocks(body: str) -> list[Block]:
    """Reject unknown marker content; never infer a legacy block past its list.

    Historical writes appended contracts in body order. The last generated
    block is therefore the newest, including when a later receipt owns it.
    Blank-line-separated prose and manual lists are outside legacy boundaries.
    """
    masked = _visible_body(body)
    markers = list(_MARKER.finditer(masked))
    if len(markers) != len(re.findall(r"(?m)^[ \t]*<!-- /?factory-gate:", masked)):
        raise ValueError("malformed gate marker")
    result = []
    index = 0
    while index < len(markers):
        opening = markers[index]
        if opening["close"]:
            raise ValueError("orphan gate closing marker")
        start = opening.end()
        if body[start : start + 1] != "\n":
            raise ValueError("gate content must start on a new line")
        if opening["hash"]:
            # The old writer emitted exactly one line of scope then one or
            # more unchecked, single-line acceptance items. Stop immediately
            # at a blank or non-list line; never consume surrounding prose.
            end = start + 1
            lines = []
            for line in body[end:].splitlines(keepends=True):
                text = line.removesuffix("\n")
                if lines and not re.fullmatch(r"- \[ \] \S.*", text):
                    break
                lines.append(text)
                end += len(line)
            if not _contract(lines):
                raise ValueError("unrecognized legacy gate contract")
            if body[end - 1 : end] == "\n":
                end -= 1
            identity = opening["hash"]
            index += 1
        else:
            if index + 1 == len(markers):
                raise ValueError("missing gate closing marker")
            closing = markers[index + 1]
            if (
                not closing["close"]
                or closing["hash"]
                or closing["task"] != opening["task"]
            ):
                raise ValueError("nested or mismatched gate boundaries")
            content = body[start + 1 : closing.start()].removesuffix("\n")
            audit, _, text = content.partition("\n")
            match = _AUDIT.fullmatch(audit)
            lines = text.split("\n")
            if not match or not _contract(lines):
                raise ValueError("unrecognized bounded gate contract")
            identity = match[1]
            end = closing.end()
            index += 2
        result.append(
            Block(opening.start(), end, opening["task"], identity, "\n".join(lines))
        )
    if any(a.end > b.start for a, b in pairwise(result)):
        raise ValueError("overlapping gate blocks")
    return result


def rewrite(body: str, replacement: str | None = None) -> tuple[str, int]:
    """Collapse all generated contracts, preserving every byte outside them.

    Without a replacement, retain the last contract for manual cleanup.
    Receipt history and decision comments are deliberately outside this API.
    """
    found = blocks(body)
    if not found:
        return (body + "\n\n" + replacement if replacement else body), 0
    newest = found[-1]
    current = replacement or render(newest.task, newest.identity, newest.text)
    updated = body
    for block in reversed(found):
        updated = (
            updated[: block.start]
            + (current if block == newest else "")
            + updated[block.end :]
        )
    return updated, len(found)
