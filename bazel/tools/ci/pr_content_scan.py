"""Warn on secret patterns in PR additions and PR commit messages."""

import os
import re
import subprocess
import sys

from public_content import INTERNAL_MARKERS

RULES = (
    (
        "GitHub token",
        re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})"),
    ),
    ("AWS access key", re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}")),
    ("private key header", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    *INTERNAL_MARKERS,
)


def git(*args):
    return subprocess.check_output(["git", *args])


def report(path, content):
    labels = [label for label, pattern in RULES if pattern.search(content)]
    if labels:
        if any(pattern.search(path) for _, pattern in RULES):
            path = "[redacted path]"
        for label in labels:
            print(f"warning: {ascii(path)}: {label}")


def scan(base, head):
    start = git("merge-base", base, head).decode().strip()
    diff = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames"]
    paths = git(*diff, "--name-only", "--diff-filter=AMT", "-z", start, head, "--")
    for raw in paths.split(b"\0")[:-1]:
        path = os.fsdecode(raw)
        patch = git("--literal-pathspecs", *diff, "-U0", start, head, "--", path)
        in_hunk = False
        additions = []
        for line in patch.decode(errors="replace").split("\n"):
            if line.startswith("@@ "):
                in_hunk = True
            elif in_hunk and line.startswith("+"):
                additions.append(line[1:])
        report(path, "\n".join(additions))

    messages = git("log", "-z", "--format=%H%x00%B", f"{start}..{head}", "--")
    records = iter(messages.decode(errors="replace").split("\0")[:-1])
    for commit, message in zip(records, records):
        report(f"commit {commit}", message)


if __name__ == "__main__":
    scan(*sys.argv[1:])
