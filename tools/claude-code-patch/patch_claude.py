#!/usr/bin/env python3
"""Apply the experimental late-bound stream-json resume patch."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


VERSION_MARKER = b'VERSION:"2.1.220"'
CLI_MARKER = b"/$bunfs/root/src/entrypoints/cli.js\x00// @bun @bytecode @bun-cjs\n"
NEXT_FILE_MARKER = (
    b"\x00/$bunfs/root/image-processor.js\x00// @bun @bytecode @bun-cjs\n"
)
PATCHES = (
    (
        b"let A=t(),{messages:I,turnInterruptionState:D",
        b"""if(!l.resume&&!l.continue&&p&&y instanceof XMn){let ve=await y.structuredInput.next();if(!ve.done&&ve.value){if(ve.value.type==="user"){let Ae=ve.value.session_id??ve.value.sessionId;if(typeof Ae==="string"&&Ae.length>0)l.resume=Ae}y.prependedLines.unshift(Ie(ve.value)+"\\n")}}""",
    ),
    (
        b"let C=t(),{messages:I,turnInterruptionState:R",
        b"""if(!l.resume&&!l.continue&&p&&_ instanceof JOn){let Ae=await _.structuredInput.next();if(!Ae.done&&Ae.value){if(Ae.value.type==="user"){let Ce=Ae.value.session_id??Ae.value.sessionId;if(typeof Ce==="string"&&Ce.length>0)l.resume=Ce}_.prependedLines.unshift(Ie(Ae.value)+"\\n")}}""",
    ),
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def delete_comment_bytes(source: bytearray, count: int) -> None:
    remaining = count
    cursor = 0
    while remaining:
        line_end = source.find(b"\n", cursor)
        if line_end < 0:
            raise RuntimeError("ran out of comment lines while preserving bundle size")
        line = bytes(source[cursor:line_end])
        leading = len(line) - len(line.lstrip())
        if not line[leading:].startswith(b"//"):
            cursor = line_end + 1
            continue
        start = cursor + leading + 2
        available = line_end - start
        take = min(remaining, available)
        del source[start : start + take]
        remaining -= take
        cursor = start


def patch_binary(input_path: Path, output_path: Path) -> None:
    original = input_path.read_bytes()
    if VERSION_MARKER not in original:
        raise RuntimeError("input is not Claude Code 2.1.220")

    cli_marker = original.find(CLI_MARKER)
    if cli_marker < 0:
        raise RuntimeError("embedded cli.js marker not found")
    source_start = cli_marker + len(CLI_MARKER)
    source_end = original.find(NEXT_FILE_MARKER, source_start)
    if source_end < 0:
        raise RuntimeError("embedded cli.js end marker not found")

    source = bytearray(original[source_start:source_end])
    original_source_size = len(source)
    selected_patch = None
    anchor = -1
    for candidate_anchor, candidate_patch in PATCHES:
        if source.count(candidate_patch) > 0:
            raise RuntimeError("patch already present")
        candidate_offset = source.find(candidate_anchor)
        if candidate_offset >= 0:
            selected_patch = candidate_patch
            anchor = candidate_offset
            break
    if selected_patch is None:
        raise RuntimeError("known runHeadless loadInitialMessages anchor not found")

    comment_start = source.find(b"// Claude Code is")
    if comment_start < 0:
        raise RuntimeError("safe comment padding region not found")
    source[anchor:anchor] = selected_patch
    delete_comment_bytes(source, len(selected_patch))
    if len(source) != original_source_size:
        raise RuntimeError(
            f"source length changed ({original_source_size} -> {len(source)}); "
            "refusing to alter Bun offsets"
        )

    patched = bytearray(original)
    patched[source_start:source_end] = source
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(patched)
    print(f"input_sha256={sha256(original)}")
    print(f"output_sha256={sha256(patched)}")
    print(f"source_offset={source_start}")
    print(f"source_size={len(source)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    patch_binary(args.input, args.output)


if __name__ == "__main__":
    main()
