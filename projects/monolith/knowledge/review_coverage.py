"""Default-off exact literal witnesses and bounded, content-based file identities.

Only the closed literal-presence grammar can use incomplete file coverage. Every
other claim still requires complete files. Nothing here infers absence or source
behavior from a slice. Limits include JSON escaping and UTF-8 serialization.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from pathlib import PurePosixPath
from urllib.parse import quote

from core.github import GITHUB_API, _github_headers

from knowledge.review_sources import EvidenceUnavailable, EvidenceUnsupported

VERSION = "literal-witness/v1"
MAX_FILE_BYTES = 128_000
MAX_RESPONSE_BYTES = 192_000
MAX_TRANSFER_BYTES = 512_000
MAX_WITNESSES = 12
_SHA = re.compile(r"[0-9a-f]{40}")
_CLAUSE = re.compile(
    r"`((?:projects|docs|bazel)/[\w./-]+\.[A-Za-z0-9]+)` contains the exact text "
    r"`([^`\r\n]{8,2000})`\."
)


def enabled():
    return os.environ.get("KG_FACT_REVIEW_COVERAGE_ENABLED", "false").lower() == "true"


def serialized_size(value):
    try:
        return len(json.dumps(value, sort_keys=True).encode("utf-8"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise EvidenceUnsupported("unsupported JSON representation") from exc


def read_response(client, path, *, deadline_at=None, charge=None):
    """Bound wire bytes before decoding, including unsuccessful responses."""
    headers = {**_github_headers(), "Accept-Encoding": "identity"}
    deadline_at = deadline_at if deadline_at is not None else time.monotonic() + 90
    remaining = deadline_at - time.monotonic()
    if remaining <= 0:
        raise EvidenceUnavailable("source collection deadline exhausted")
    with client.stream(
        "GET", GITHUB_API + path, headers=headers, timeout=min(10, remaining)
    ) as response:
        if response.status_code != 200:
            raise EvidenceUnavailable(f"GitHub returned {response.status_code}")
        if response.headers.get("content-encoding", "identity") != "identity":
            raise EvidenceUnsupported("unsupported source transport encoding")
        data = bytearray()
        for chunk in response.iter_raw(chunk_size=8192):
            if time.monotonic() >= deadline_at:
                raise EvidenceUnavailable("source collection deadline exhausted")
            if charge is not None:
                charge(len(chunk))
            if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                raise EvidenceUnsupported("source response byte bound exceeded")
            data.extend(chunk)
        try:
            result = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise EvidenceUnsupported("unsupported source JSON encoding") from exc
        if serialized_size(result) > MAX_RESPONSE_BYTES:
            raise EvidenceUnsupported("source response serialization bound exceeded")
        return result


def file_identity(paths, main, get):
    """Non-recursive trees avoid transferring blobs just to probe identities.

    Directory trees are read at the pinned commit. Tree hashes and main itself
    are intentionally absent from the identity: unrelated changes cannot retry
    a blocked fact. A truncated tree cannot establish a missing file.
    """
    directories, files = {}, {}
    for path in paths:
        directory, name = str(PurePosixPath(path).parent), PurePosixPath(path).name
        if directory not in directories:
            tree = get(f"/git/trees/{main}:{quote(directory, safe='/')}")
            entries = tree.get("tree")
            if (
                tree.get("truncated") is True
                or isinstance(entries, list)
                and len(entries) > 1000
            ):
                raise EvidenceUnsupported("source identity tree exceeds coverage bound")
            if (
                tree.get("truncated") is not False
                or not isinstance(entries, list)
                or any(not isinstance(entry, dict) for entry in entries)
            ):
                raise EvidenceUnavailable("incomplete source identity tree")
            directories[directory] = entries
        matches = [
            entry for entry in directories[directory] if entry.get("path") == name
        ]
        if len(matches) != 1:
            raise EvidenceUnavailable("missing or ambiguous source identity")
        entry = matches[0]
        if (
            not _SHA.fullmatch(str(entry.get("sha")))
            or entry.get("type") == "blob"
            and (type(entry.get("size")) is not int or entry["size"] < 0)
        ):
            raise EvidenceUnavailable("malformed source identity")
        files[path] = {key: entry.get(key) for key in ("sha", "size", "type", "mode")}
    return {"version": VERSION, "files": files}


def literal_clauses(claim):
    """Full-match BOTH fields; no dropped prose, qualifiers or extra clauses."""
    clauses = []
    for field in ("title", "content"):
        value = claim.get(field) or ""
        for line in value.splitlines():
            if not line.strip():
                continue
            match = _CLAUSE.fullmatch(line.strip())
            if match is None or ".." in PurePosixPath(match[1]).parts:
                return None
            clauses.append((match[1], match[2]))
    return clauses if 0 < len(clauses) <= MAX_WITNESSES else None


def decode_blob(body, identity):
    if identity["type"] != "blob" or identity["mode"] not in {"100644", "100755"}:
        raise EvidenceUnsupported("unsupported source file type")
    if identity["size"] > MAX_FILE_BYTES:
        raise EvidenceUnsupported("source decode byte bound exceeded")
    if body.get("sha") != identity["sha"] or body.get("size") != identity["size"]:
        raise EvidenceUnavailable("source blob identity mismatch")
    if body.get("encoding") != "base64" or not isinstance(body.get("content"), str):
        raise EvidenceUnsupported("unsupported source file encoding")
    encoded = body["content"]
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise EvidenceUnsupported("source encoded byte bound exceeded")
    encoded = encoded.replace("\n", "")
    if len(encoded) > ((identity["size"] + 2) // 3) * 4:
        raise EvidenceUnsupported("source decoded byte bound exceeded")
    try:
        raw = base64.b64decode(encoded, validate=True)
        content = raw.decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise EvidenceUnsupported("invalid source file encoding") from exc
    if (
        len(raw) != identity["size"]
        or hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        != identity["sha"]
    ):
        raise EvidenceUnavailable("source blob content mismatch")
    if "\x00" in content:
        raise EvidenceUnsupported("unsupported binary source")
    return content


def witnesses(claim, contents, identity, repo, main):
    clauses = literal_clauses(claim)
    if clauses is None or {path for path, _ in clauses} != set(contents):
        raise EvidenceUnsupported(
            "incomplete coverage: complete literal-presence claims required"
        )
    sources = []
    for path, literal in dict.fromkeys(clauses):
        raw, needle = contents[path].encode("utf-8"), literal.encode("utf-8")
        start = raw.find(needle)
        if start < 0:
            # Even a complete scan proving no literal cannot disprove a broader
            # claim. The partial-coverage path only produces positive evidence.
            raise EvidenceUnsupported(
                "incomplete coverage: a required literal has no witness"
            )
        end = start + len(needle)
        first = raw[:start].count(b"\n") + 1
        last = raw[:end].count(b"\n") + 1
        sources.append(
            {
                "id": f"s{len(sources) + 1}",
                "subject": f"file:{path}",
                "url": f"https://github.com/{repo}/blob/{main}/{path}#L{first}-L{last}",
                "text": literal,
                "coverage": {
                    "kind": VERSION,
                    "identity": identity,
                    "start_byte": start,
                    "end_byte": end,
                    "start_line": first,
                    "end_line": last,
                    "omitted_bytes": [
                        [a, b] for a, b in ((0, start), (end, len(raw))) if a < b
                    ],
                },
            }
        )
    return sources


def complete_witness_citations(claim, sources, citations):
    """Every literal, including repeated-file clauses, needs its own quotation."""
    clauses = literal_clauses(claim)
    if clauses is None:
        return False
    required = set(clauses)
    witnessed = {
        (source["subject"].removeprefix("file:"), source["text"])
        for source in sources
        if source.get("coverage", {}).get("kind") == VERSION
        and any(
            c.source_id == source["id"] and c.quote == source["text"] for c in citations
        )
    }
    return required == witnessed
