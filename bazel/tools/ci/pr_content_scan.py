"""Fail on secrets and private markers in PR additions and PR commit messages."""

# invariant: ROOT-5
import codecs
import difflib
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


# Accept complete variable expansions and the Renovate mounted-token read.
# Literal prefixes, suffixes and nonempty fallback values still need review.
VARIABLE = r"[a-zA-Z_][a-zA-Z_0-9]*"
EXPANSION = rf"\$(?:{VARIABLE}|\{{{VARIABLE}(?::-)?\}})"
RENOVATE_TOKEN_READ = r'"\$\(cat /var/run/renovate-token/token\)"'
RUNTIME_VALUE = re.compile(
    rf'(?:{EXPANSION}|"{EXPANSION}"|{RENOVATE_TOKEN_READ})(?=\s|$)'
)


def matches(label, pattern, content):
    if label != "secret env var assignment":
        return pattern.search(content)
    # The shared assignment regex consumes the first character of the value.
    position = 0
    while match := pattern.search(content, position):
        reference = RUNTIME_VALUE.match(content, match.end() - 1)
        if reference is None:
            return True
        position = reference.end()
    return False


def git(*args):
    return subprocess.check_output(["git", *args])


def report(path, content):
    labels = [label for label, pattern in RULES if matches(label, pattern, content)]
    if labels:
        if any(pattern.search(path) for _, pattern in RULES):
            path = "[redacted path]"
        for label in labels:
            print(f"error: {ascii(path)}: {label}")
    return bool(labels)


def blob_lines(mode, oid):
    if mode in (b"000000", b"160000"):
        return []
    data = git("cat-file", "blob", oid)
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace").splitlines()
    if b"\0" in data:
        # BOM-less UTF-16 configuration text commonly has an ASCII first line.
        sample = data[:256]
        for offset, encoding in ((1, "utf-16-le"), (0, "utf-16-be")):
            lane = sample[offset::2]
            if (
                lane
                and lane.count(0) > len(lane) // 2
                and 0 not in sample[1 - offset :: 2]
            ):
                return data.decode(encoding, errors="replace").splitlines()
        return []
    return data.decode("utf-8-sig", errors="replace").splitlines()


def scan(base, head):
    found = False
    start = git("merge-base", base, head).decode().strip()
    changes = git(
        "diff",
        "--raw",
        "--no-abbrev",
        "--no-renames",
        "--diff-filter=AMT",
        "-z",
        start,
        head,
        "--",
    )
    records = iter(changes.split(b"\0")[:-1])
    for header, path in zip(records, records):
        old_mode, new_mode, old_oid, new_oid, _ = header[1:].split()
        before = blob_lines(old_mode, old_oid)
        after = blob_lines(new_mode, new_oid)
        additions = []
        for op, _, _, first, last in difflib.SequenceMatcher(
            None, before, after, autojunk=False
        ).get_opcodes():
            if op in ("replace", "insert"):
                additions.extend(after[first:last])
        found |= report(os.fsdecode(path), "\n".join(additions))

    messages = git("log", "-z", "--format=%H%x00%B", f"{start}..{head}", "--")
    records = iter(messages.decode(errors="replace").split("\0")[:-1])
    for commit, message in zip(records, records):
        found |= report(f"commit {commit}", message)
    return found


if __name__ == "__main__":
    sys.exit(scan(*sys.argv[1:]))
