"""Build and commit a v1 storyboard snapshot to an S3-compatible store."""

from datetime import timedelta
import hashlib
import json
import re
import subprocess


def encode(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def gitleaks(body):
    result = subprocess.run(
        ["gitleaks", "stdin", "--redact", "--exit-code", "42"],
        input=body,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
    )
    if result.returncode == 42:
        return True
    result.check_returncode()
    return False


def public_issue(issue, projects):
    return issue["project"] in projects and all(
        label.casefold() != "private" for label in issue["labels"]
    )


def redact(text, user_ids):
    text = re.sub(r"[\w.!#$%&'*+/=?^_`{|}~-]+@[\w.-]+", "[redacted]", text)
    for identifier in sorted(set(user_ids), key=len, reverse=True):
        if identifier:
            text = re.sub(rf"(?<!\w){re.escape(identifier)}(?!\w)", "[redacted]", text)
    return text


def public_turn(turn, user_ids, scan=gitleaks):
    if turn["completed"] is not True:
        return None
    header = {key: turn[key] for key in ("number", "at", "role")}
    content = {
        "text": redact(turn["text"], user_ids),
        "toolCalls": [
            {key: redact(call[key], user_ids) for key in ("name", "input", "output")}
            for call in turn["toolCalls"]
        ],
    }
    if "diff" in turn:
        content["diff"] = redact(turn["diff"], user_ids)
    if scan(encode(content)):
        return {**header, "withheld": True}
    return {**header, "withheld": False, **content}


def publish(s3, bucket, now, story, goals, issues, sessions, scan=gitleaks):
    """Commit pre-sanitized documents, then expire unreachable objects."""
    live = set()

    def write(key, body):
        if scan(body):
            raise ValueError("secret detected in public object")
        s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")

    def put(document):
        body = encode(document)
        key = f"objects/{hashlib.sha256(body).hexdigest()}.json"
        if key not in live:
            write(key, body)
            live.add(key)
        return key

    manifest = {
        "version": 1,
        "generatedAt": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "story": put(story),
    }
    for name, records in (("goals", goals), ("issues", issues), ("sessions", sessions)):
        manifest[name] = {key: put(records[key]) for key in sorted(records)}
    write("manifest.json", encode(manifest))
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
                s3.delete_object(Bucket=bucket, Key=key)
