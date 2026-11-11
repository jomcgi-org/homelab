#!/usr/bin/env python3
"""Read bounded pr-checks evidence using gh and the credential-free log RPC.

Wire contract: buildbuddy-io/buildbuddy proto/eventlog.proto and
server/eventlog/eventlog.go. A stored chunk's live=false does not mean the
invocation finished. Only the exact commit's fresh GitHub status says that.
"""

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

LOG_ENDPOINT = "https://jomcgi.buildbuddy.io/rpc/BuildBuddyService/GetEventLogChunk"
TARGET = re.compile(
    r"https://jomcgi\.buildbuddy\.io/invocation/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
CHUNK_ID = re.compile(r"[0-9a-f]{4}")
MAX_CHUNKS = 16
MAX_BYTES = 1024 * 1024
MAX_RESPONSE = 2 * MAX_BYTES
MAX_OUTPUT = 64 * 1024
TIMEOUT = 10
CACHE_TTL = 3600
CACHE_FORMAT = 2  # Older entries may retain a credential split at a chunk limit.
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
EVIDENCE = re.compile(
    r"FAILED|FAIL:|ERROR|Error:|Traceback|AssertionError|E +assert|Executed \d+ out of \d+ tests?"
)
TEST_COUNT = re.compile(r"Executed \d+ out of \d+ tests?")


class EvidenceError(Exception):
    pass


def redact(value):
    """Remove common credentials before selecting excerpts or caching text."""
    value = ANSI.sub("", value).replace("\r", "\n")
    patterns = (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})",
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
        r"(?i)(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}",
        r"(?i)\b[\w-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|authorization|credential|signature)[\w-]*[\"']?\s*[=:]\s*[\"']?[^\s\"']+",
        r"(?i)--?(?:password|passwd|token|secret|api-?key)(?:\s+|=)[^\s\"']+",
        r"://[^/\s:@]+:[^@\s]+@",
    )
    for pattern in patterns:
        value = re.sub(pattern, "[REDACTED]", value)
    # Do not pass arbitrary terminal control bytes through from remote logs.
    return "".join(c for c in value if c in "\n\t" or ord(c) >= 32 and ord(c) != 127)


def read_status(repo, sha):
    # gh keeps the existing GitHub identity and guest proxy opt-in. This helper
    # never reads a token, supplies a BuildBuddy key or changes auth config.
    query = '{sha, statuses: [.statuses[] | select(.context == "pr-checks") | {id, state, target_url}]}'
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--hostname",
                "github.com",
                "--method",
                "GET",
                f"repos/{repo}/commits/{sha}/status?per_page=100",
                "--jq",
                query,
            ],
            capture_output=True,
            timeout=TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise EvidenceError(
            "GitHub status read failed; check the existing gh connection"
        ) from None
    if result.returncode or len(result.stdout) > MAX_RESPONSE:
        raise EvidenceError("GitHub status unavailable; no CI outcome inferred")
    try:
        response = json.loads(result.stdout)
        if response["sha"] != sha:
            raise ValueError("commit changed")
        statuses = response["statuses"]
        if not statuses:
            raise EvidenceError("No pr-checks status for this exact commit")
        if not isinstance(statuses, list) or len(statuses) > 100:
            raise ValueError("invalid statuses")
        if any(type(item["id"]) is not int for item in statuses):
            raise ValueError("invalid status identity")
        status = max(statuses, key=lambda item: item["id"])
        match = TARGET.fullmatch(status["target_url"])
        if match is None or status["state"] not in {
            "pending",
            "success",
            "failure",
            "error",
        }:
            raise ValueError("unsupported status")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise EvidenceError(
            "Malformed pr-checks status or unsupported log URL"
        ) from None
    return {"id": status["id"], "state": status["state"], "invocation": match[1]}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EvidenceError(
            "Log endpoint redirected; no alternate destination contacted"
        )


def fetch_chunk(invocation, chunk_id):
    request = urllib.request.Request(
        LOG_ENDPOINT,
        data=json.dumps(
            {"invocationId": invocation, "chunkId": chunk_id, "minLines": 0}
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(
            request, timeout=TIMEOUT
        ) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise EvidenceError("Log response exceeded the byte bound")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise TypeError("not an object")
        return value
    except (OSError, urllib.error.URLError, ValueError, TypeError):
        raise EvidenceError("Log read failed; no CI outcome inferred") from None


def read_log(invocation, fetch=fetch_chunk, max_chunks=MAX_CHUNKS):
    """Walk backwards one stored chunk at a time, retaining chronological text."""
    pieces, visited = [], set()
    cursor, size, reason, live = "", 0, None, False
    for _ in range(max_chunks):
        if cursor in visited:
            raise EvidenceError("Repeated log cursor; traversal stopped")
        visited.add(cursor)
        value = fetch(invocation, cursor)
        try:
            if type(value.get("live", False)) is not bool:
                raise ValueError("invalid live marker")
            live = live or value.get("live", False)
            previous = value.get("previousChunkId", "")
            if (
                not isinstance(previous, str)
                or previous
                and not CHUNK_ID.fullmatch(previous)
            ):
                raise ValueError("invalid cursor")
            data = base64.b64decode(value.get("buffer", ""), validate=True)
        except (ValueError, TypeError, binascii.Error, AttributeError):
            raise EvidenceError("Malformed log chunk") from None
        if size + len(data) > MAX_BYTES:
            remaining = MAX_BYTES - size
            pieces.append(data[-remaining:] if remaining else b"")
            reason = "byte_limit"
            break
        pieces.append(data)
        size += len(data)
        cursor = previous
        if not cursor:
            break
    else:
        if cursor:
            reason = "chunk_limit"
    raw = b"".join(reversed(pieces))
    if reason:
        # Any retained boundary can split a credential from its recognizable
        # prefix, including a line spanning several chunks or a byte limit
        # with no room for the oldest chunk. Trim only after joining them.
        raw = raw.partition(b"\n")[2]
        begin = re.search(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----", raw)
        end = re.search(rb"-----END [A-Z ]*PRIVATE KEY-----", raw)
        if end and (begin is None or end.start() < begin.start()):
            raw = b"[REDACTED]\n" + raw[end.end() :].lstrip(b"\r\n")
    text = redact(raw.decode("utf-8", errors="replace"))
    return {
        "text": text,
        "chunks": len(pieces),
        "truncated": reason,
        "live": live,
        "bytes": sum(map(len, pieces)),
    }


def cache_directory():
    root = Path(tempfile.gettempdir()) / f"ci-log-{os.getuid()}"
    try:
        root.mkdir(mode=0o700, exist_ok=True)
        stat = root.lstat()
        if (
            not root.is_dir()
            or root.is_symlink()
            or stat.st_uid != os.getuid()
            or stat.st_mode & 0o077
        ):
            return None
    except OSError:
        return None
    return root


def cached_log(repo, sha, status, *, cache=None, fetch=fetch_chunk):
    key = hashlib.sha256(
        json.dumps([CACHE_FORMAT, repo, sha, status], sort_keys=True).encode()
    ).hexdigest()
    path = cache / (key + ".json") if cache else None
    terminal = status["state"] != "pending"
    if terminal and path:
        try:
            if not path.is_symlink() and time.time() - path.stat().st_mtime < CACHE_TTL:
                with path.open("rb") as source:
                    raw = source.read(2 * MAX_BYTES + 1)
                if len(raw) <= 2 * MAX_BYTES:
                    value = json.loads(raw)
                    if (
                        isinstance(value["text"], str)
                        and type(value["chunks"]) is int
                        and type(value["bytes"]) is int
                        and value.get("live") is False
                        and value.get("truncated")
                        in {None, "byte_limit", "chunk_limit"}
                    ):
                        value["text"] = redact(value["text"])
                        return value, True
        except (OSError, ValueError, KeyError, TypeError):
            pass
    value = read_log(status["invocation"], fetch)
    if terminal and path and value["text"] and not value["live"]:
        # Bound disk use, cache only already-redacted evidence, and key by the
        # fresh status ID as well as invocation so a rerun cannot reuse it.
        try:
            entries = sorted(
                cache.glob("*.json"), key=lambda entry: entry.lstat().st_mtime
            )
            for old in entries[:-31]:
                old.unlink()
            with tempfile.NamedTemporaryFile(
                mode="w", dir=cache, delete=False
            ) as handle:
                temporary = Path(handle.name)
                json.dump(value, handle)
            temporary.replace(path)
        except OSError:
            pass  # Cache failure never hides the freshly fetched evidence.
    return value, False


def excerpts(text):
    """Keep complete selected windows and the tail; label every omitted region."""
    lines = text.splitlines(keepends=True)
    sizes = [len(line.encode()) for line in lines]
    tail_start = max(0, len(lines) - 60)
    while sum(sizes[tail_start:]) > MAX_OUTPUT // 4:
        tail_start += 1
    selected = set(range(tail_start, len(lines)))
    used = sum(sizes[tail_start:])
    summaries = [i for i, line in enumerate(lines) if TEST_COUNT.search(line)]
    summary_used = 0
    for i in reversed(summaries):
        if i not in selected and summary_used + sizes[i] <= MAX_OUTPUT // 8:
            selected.add(i)
            summary_used += sizes[i]
            used += sizes[i]
    windows = [
        (max(0, i - 12), min(len(lines), i + 26))
        for i, line in enumerate(lines)
        if EVIDENCE.search(line)
    ]
    limited = tail_start > max(0, len(lines) - 60)
    for start, end in windows:
        additional = set(range(start, end)) - selected
        size = sum(sizes[i] for i in additional)
        # Reserve space for omission markers as well as log bytes. A whole
        # selected window fits or is omitted; never clip an assertion in half.
        if used + size > MAX_OUTPUT // 2:
            limited = True
            continue
        selected.update(additional)
        used += size
    output, previous = [], -1
    for i in sorted(selected):
        if i > previous + 1:
            output.append(f"[{i - previous - 1} log lines omitted]\n")
        output.append(lines[i])
        previous = i
    if limited:
        output.append("[some context omitted: output byte limit]\n")
    if any(i not in selected for i in summaries):
        output.append("[some test-count summaries omitted: summary byte limit]\n")
    return "".join(output) or "[no log text available]\n"


def inspect(
    repo,
    sha,
    *,
    wait=0,
    status_reader=read_status,
    fetch=fetch_chunk,
    cache=None,
    sleep=time.sleep,
    clock=time.monotonic,
):
    deadline, delay = clock() + wait, 2
    while True:
        status = status_reader(repo, sha)
        # Waiting polls only status. Fetch logs once, at terminal or at the bound.
        remaining = deadline - clock()
        if status["state"] != "pending" or remaining < delay:
            break
        sleep(delay)
        delay = min(delay * 2, 16)
    log, hit = cached_log(repo, sha, status, cache=cache, fetch=fetch)
    phase = "running" if status["state"] == "pending" else "terminal"
    coverage = (
        "no stored log text available"
        if not log["text"]
        else log["truncated"] or "available stored log scanned"
    )
    return (
        f"pr-checks {phase}: {status['state']} | {repo}@{sha}\n"
        f"https://jomcgi.buildbuddy.io/invocation/{status['invocation']}\n"
        f"Evidence: {log['chunks']} chunks, {log['bytes']} bytes; {coverage}; "
        f"{'cached' if hit else 'fetched'}; failure context, test counts and tail below.\n"
        "Status is not a test count; inspect the Executed N out of M tests line.\n"
        + excerpts(log["text"])
    ), status["state"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sha", help="full 40-character commit SHA")
    parser.add_argument("--repo", default="jomcgi-org/homelab")
    parser.add_argument(
        "--wait", type=int, default=0, help="poll status for up to 60 seconds"
    )
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo
    ):
        parser.error("supply an exact commit SHA and owner/repository")
    if not 0 <= args.wait <= 60:
        parser.error("--wait must be between 0 and 60 seconds")
    try:
        output, state = inspect(
            args.repo, args.sha, wait=args.wait, cache=cache_directory()
        )
        print(output, end="" if output.endswith("\n") else "\n")
        return 0 if state == "success" else 1 if state in {"failure", "error"} else 2
    except EvidenceError as exc:
        print(f"ci-log: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
