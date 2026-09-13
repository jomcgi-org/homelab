"""Keep import-only package moves out of the durable workflow fingerprint."""

from __future__ import annotations

import ast
import re
import textwrap

_LEGACY_PACKAGES = {
    "factory.execution": "agent_sessions",
    "factory.orchestration": "swarm",
}


def durable_source(source: str) -> str:
    """Normalize only relocated absolute from-import module names.

    Preserve all other source bytes, including comments and string literals:
    changing workflow logic or a checkpoint must still change its version.
    AST locations identify imports; the replacement touches only their module
    token, even for multiline imports and nested helpers.
    """
    dedented = textwrap.dedent(source)
    lines = dedented.splitlines(keepends=True)
    original = source.splitlines(keepends=True)
    edits = []
    for node in ast.walk(ast.parse(dedented)):
        if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
            continue
        for package, legacy in _LEGACY_PACKAGES.items():
            if node.module != package and not node.module.startswith(package + "."):
                continue
            line_index = node.lineno - 1
            # AST columns are UTF-8 bytes, so operate on encoded lines.
            line = lines[line_index].encode("utf-8")
            match = re.match(
                rb"from\s+(" + re.escape(node.module.encode()) + rb")\b",
                line[node.col_offset :],
            )
            if match is None:
                raise ValueError("cannot locate durable import module")
            indent = len(original[line_index].encode()) - len(line)
            start = indent + node.col_offset + match.start(1)
            end = indent + node.col_offset + match.end(1)
            replacement = legacy + node.module[len(package) :]
            edits.append((line_index, start, end, replacement.encode()))
            break
    encoded = [line.encode("utf-8") for line in original]
    for line, start, end, replacement in sorted(edits, reverse=True):
        encoded[line] = encoded[line][:start] + replacement + encoded[line][end:]
    return b"".join(encoded).decode("utf-8")
