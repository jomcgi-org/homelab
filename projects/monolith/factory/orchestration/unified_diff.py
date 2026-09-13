from __future__ import annotations

import ast


def _path(value: str) -> str:
    value = value.strip()
    if value == "/dev/null":
        return value
    if value.startswith('"') and value.endswith('"'):
        try:
            value = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            value = value[1:-1]
    if value.startswith(("a/", "b/")):
        return value[2:]
    return value


def _header_paths(line: str) -> tuple[str, str]:
    value = line[len("diff --git ") :].rstrip("\r\n")
    if value.startswith('"'):
        try:
            old, new = value.split('" "', 1)
            return _path(old + '"'), _path('"' + new)
        except ValueError:
            return "", ""
    marker = " b/"
    if marker not in value:
        return "", ""
    old, new = value.rsplit(marker, 1)
    return _path(old), _path("b/" + new)


def parse_unified_diff(diff: str) -> list[dict]:
    """Parse git unified diff text into per-file metadata and hunk text."""
    lines = diff.splitlines(keepends=True)
    starts = [
        index for index, line in enumerate(lines) if line.startswith("diff --git ")
    ]
    files = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        block = lines[start:end]
        old_path, new_path = _header_paths(block[0])
        status = "modified"
        rename_to = None
        old_header = None
        new_header = None
        additions = 0
        deletions = 0
        hunk_start = None

        for index, line in enumerate(block[1:], start=1):
            stripped = line.rstrip("\r\n")
            # Header lines only occur before the first hunk, and they must only
            # be read there. Inside a hunk the prefix character makes ordinary
            # content impersonate a header: removing a line that reads
            # "-- comment" produces "--- comment", and adding one that reads
            # "++ x" produces "+++ x". Parsing those as headers renames the file
            # to whatever the content happened to say, which puts a real hunk
            # behind the wrong path. Every migration in this repo opens with
            # "-- ", so deleting one was enough to trigger it.
            if hunk_start is None:
                if stripped.startswith("new file mode "):
                    status = "added"
                elif stripped.startswith("deleted file mode "):
                    status = "removed"
                elif stripped.startswith("rename from "):
                    status = "renamed"
                elif stripped.startswith("rename to "):
                    status = "renamed"
                    rename_to = _path(stripped[len("rename to ") :])
                elif stripped.startswith("--- "):
                    old_header = _path(stripped[4:])
                elif stripped.startswith("+++ "):
                    new_header = _path(stripped[4:])
                elif stripped.startswith("@@"):
                    hunk_start = index
            else:
                if line.startswith("+"):
                    additions += 1
                elif line.startswith("-"):
                    deletions += 1

        if old_header == "/dev/null":
            status = "added"
        elif new_header == "/dev/null":
            status = "removed"
        path = (
            rename_to
            or (old_header if status == "removed" else new_header)
            or (old_path if status == "removed" else new_path)
        )
        patch = "".join(block[hunk_start:]) if hunk_start is not None else None
        files.append(
            {
                "path": path,
                "status": status,
                "additions": additions,
                "deletions": deletions,
                "changes": additions + deletions,
                "patch": patch,
            }
        )
    return files
