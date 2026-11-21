"""Build and commit a v1 storyboard snapshot to an S3-compatible store."""

from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

# Issues with one of these labels never publish. itsaplan label names are
# compared case-insensitively. security-finding is the GitHub-era name.
WITHHELD_LABELS = frozenset({"private", "security", "security-finding", "intsec"})


def encode(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def gitleaks(bodies):
    """Return the indexes of the bodies that match the secret scan.

    One gitleaks run scans each body as its own file. A run costs about half
    a second, and one cycle can hold thousands of turns. A body is published
    text, so nothing in it can suppress a finding: the scan ignores inline
    gitleaks:allow comments, and reads a .gitleaksignore only from its own
    temporary directory, which holds none.
    """
    if not bodies:
        return set()
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp, "bodies")
        source.mkdir()
        for index, body in enumerate(bodies):
            (source / str(index)).write_bytes(body)
        report = Path(tmp, "report.json")
        result = subprocess.run(
            [
                "gitleaks",
                "dir",
                str(source),
                "--redact",
                "--no-banner",
                "--ignore-gitleaks-allow",
                "--gitleaks-ignore-path",
                tmp,
                "--report-format",
                "json",
                "--report-path",
                str(report),
                "--exit-code",
                "42",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
        )
        if result.returncode not in (0, 42):
            result.check_returncode()
        flagged = {int(Path(f["File"]).name) for f in json.loads(report.read_text())}
    if (result.returncode == 42) != bool(flagged) or not flagged <= set(
        range(len(bodies))
    ):
        raise ValueError("the secret scan report does not match its exit status")
    return flagged


def public_issue(issue, projects):
    return issue["project"] in projects and all(
        label.casefold() not in WITHHELD_LABELS for label in issue["labels"]
    )


def redact(text, user_ids):
    text = re.sub(r"[\w.!#$%&'*+/=?^_`{|}~-]+@[\w.-]+", "[redacted]", text)
    for identifier in sorted(set(user_ids), key=len, reverse=True):
        if identifier:
            text = re.sub(rf"(?<!\w){re.escape(identifier)}(?!\w)", "[redacted]", text)
    return text


def public_texts(texts, user_ids, scan=gitleaks):
    """Redact each text, or return None for a text the secret scan matches."""
    redacted = [redact(text, user_ids) for text in texts]
    flagged = scan([encode(text) for text in redacted])
    return [None if i in flagged else text for i, text in enumerate(redacted)]


def public_turns(turns, user_ids, scan=gitleaks):
    """Redact each turn and withhold every turn the secret scan matches."""
    contents = []
    for turn in turns:
        content = {
            "text": redact(turn["text"], user_ids),
            "toolCalls": [
                {k: redact(call[k], user_ids) for k in ("name", "input", "output")}
                for call in turn["toolCalls"]
            ],
        }
        if "diff" in turn:
            content["diff"] = redact(turn["diff"], user_ids)
        contents.append(content)
    flagged = scan([encode(content) for content in contents])
    public = []
    for index, (turn, content) in enumerate(zip(turns, contents)):
        header = {key: turn[key] for key in ("number", "at", "role")}
        if index in flagged:
            public.append({**header, "withheld": True})
        else:
            public.append({**header, "withheld": False, **content})
    return public


def publish(s3, bucket, now, story, goals, issues, sessions, scan=gitleaks):
    """Commit pre-sanitized documents, then expire unreachable objects."""
    objects = {}

    def put(document):
        body = encode(document)
        key = f"objects/{hashlib.sha256(body).hexdigest()}.json"
        objects[key] = body
        return key

    manifest = {
        "version": 1,
        "generatedAt": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "story": put(story),
    }
    for name, records in (("goals", goals), ("issues", issues), ("sessions", sessions)):
        manifest[name] = {key: put(records[key]) for key in sorted(records)}
    writes = [*objects.items(), ("manifest.json", encode(manifest))]
    # One scan covers every body before the first write.
    if scan([body for _, body in writes]):
        raise ValueError("secret detected in public object")
    for key, body in writes:
        s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
    live = set(objects)
    pages = s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix="objects/"
    )
    for page in pages:
        for entry in page.get("Contents", []):
            key = entry["Key"]
            if (
                key not in live
                and re.fullmatch(r"objects/[a-f0-9]{64}\.json", key)
                and now - entry["LastModified"] >= timedelta(hours=24)
            ):
                # invariant: STORYBOARD-1
                # Live puts clear this metadata when a digest returns.
                # The post-commit copy starts a fresh LastModified interval.
                head = s3.head_object(Bucket=bucket, Key=key)
                if head["Metadata"].get("dropped") == "1":
                    s3.delete_object(Bucket=bucket, Key=key)
                else:
                    s3.copy_object(
                        Bucket=bucket,
                        Key=key,
                        CopySource={"Bucket": bucket, "Key": key},
                        MetadataDirective="REPLACE",
                        Metadata={"dropped": "1"},
                        ContentType="application/json",
                    )
