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
_ITEM = re.compile(r"- \[[ xX]\] \S.*")
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
        and all(_ITEM.fullmatch(line) for line in lines[1:])
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
    if fence is not None:
        raise ValueError("unterminated fence makes gate boundaries ambiguous")
    return "".join(visible)


def unowned_rescopes(body: str, found: list[Block]) -> int:
    return sum(
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
            # more single-line acceptance items, possibly ticked since writing.
            # Stop at a blank or non-list line; never consume surrounding prose.
            end = start + 1
            lines = []
            for line in body[end:].splitlines(keepends=True):
                text = line.removesuffix("\n")
                if lines and not _ITEM.fullmatch(text):
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


def strip(body: str) -> str:
    """The issue text with every generated contract removed.

    Unrecognized marker content is left in place: hashing the whole body is
    the conservative reading when the boundaries are ambiguous.
    """
    if not re.search(r"<!--\s*/?factory-gate:", body):
        return body
    try:
        found = blocks(body)
    except ValueError:
        return body
    updated = body
    for block in reversed(found):
        updated = updated[: block.start] + updated[block.end :]
    return updated


def _rendered(replacement: str | None) -> Block | None:
    """The one block a rendered replacement holds, or None when it is not one."""
    if not replacement:
        return None
    try:
        found = blocks(replacement)
    except ValueError:
        return None
    return found[0] if len(found) == 1 else None


def _carry_ticks(old_text: str, new_text: str) -> str:
    """Tick every item in the new text that was ticked in the old one."""
    ticked = {
        line[6:]: line
        for line in old_text.split("\n")
        if line.startswith("- [x] ") or line.startswith("- [X] ")
    }
    return "\n".join(
        ticked[line[6:]] if line.startswith("- [ ] ") and line[6:] in ticked else line
        for line in new_text.split("\n")
    )


def rewrite(body: str, replacement: str | None = None) -> tuple[str, int]:
    """Collapse all generated contracts, preserving every byte outside them.

    Without a replacement, retain the last contract for manual cleanup.
    Receipt history and decision comments are deliberately outside this API.
    """
    # Marker-free prose cannot contain an owned block. Preserve the old append
    # behavior even when the human body contains an unfinished code fence.
    if not re.search(r"<!--\s*/?factory-gate:", body):
        return (body + "\n\n" + replacement if replacement else body), 0
    found = blocks(body)
    if not found:
        return (body + "\n\n" + replacement if replacement else body), 0
    newest = found[-1]
    current = replacement or render(newest.task, newest.identity, newest.text)
    replaced = _rendered(replacement)
    if replaced is not None and replaced.identity == newest.identity:
        # A retry of the same contract, however its scope line is rendered
        # now and whichever task writes it, must retain human completion ticks.
        current = render(
            replaced.task, newest.identity, _carry_ticks(newest.text, replaced.text)
        )
    updated = body
    updated = body
    for block in reversed(found):
        updated = (
            updated[: block.start]
            + (current if block == newest else "")
            + updated[block.end :]
        )
    return updated, len(found)
