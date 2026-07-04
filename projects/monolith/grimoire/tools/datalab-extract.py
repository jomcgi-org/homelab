"""Extract a book PDF with the Datalab API and feed the grimoire pipeline.

One command runs the whole intake path for a new book:

  1. POST the PDF to the Datalab convert API (marker, ``output_format=json``)
     and poll ``request_check_url`` until the parse completes.
  2. Unwrap the response envelope into the bare Marker folder layout the rest
     of the tooling expects: ``output.json`` (the block tree), ``output.
     metadata.json``, cropped images as sibling files, plus ``source.pdf``.
  3. rclone-copy that folder to the Google Drive books folder FIRST, so the
     extraction is offsite before anything else happens.
  4. Convert to chunk NDJSON with ``marker.py`` (sibling module).
  5. Upload raw + chunks to ``s3://grimoire/books/<book-id>/`` with
     ``upload-book.sh`` (sibling script; does its own kubectl port-forward).

Auth is the ``DATALAB_API_KEY`` env var (dashboard: https://www.datalab.to).
Every stage is idempotent and skippable: an existing ``output.json`` skips the
paid API call (``--force-extract`` overrides), rclone copy re-syncs cheaply,
and the S3 uploader overwrites the same keys. Re-running after a failure
resumes where it stopped. Duplicate credit spend is guarded twice: a
``.datalab-request.json`` checkpoint written at submit time lets an
interrupted run re-attach to its in-flight request instead of resubmitting,
and a completed ``output.json`` short-circuits the API stage entirely. Only
``--force-extract`` re-spends credits deliberately.

The default ``--mode accurate`` matches the "marker high accuracy" requests
used for the existing corpus. Chunks land in Postgres at the next daily
``grimoire-load-chunks`` run (02:00 UTC), which also auto-registers the book
row; entity extraction stays manual (submit a Workflow from
``cronworkflow/grimoire-extract-entities`` with ``GRIMOIRE_EXTRACT_BOOK``).

Stdlib only, like ``marker.py``: run it with any python3, no venv needed.

Usage:
  DATALAB_API_KEY=... python3 datalab-extract.py <book-id> <pdf> [options]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DATALAB_CONVERT_URL = "https://www.datalab.to/api/v1/convert"
DEFAULT_DRIVE_REMOTE = "jomcgi"
# The shared "books" folder on Drive: extraction folders live at its top level.
DEFAULT_DRIVE_FOLDER_ID = "1xa32QQR3ZfxYWd4MpxWZe_Q6_7a4rtih"

_BOOK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_TOOLS_DIR = Path(__file__).resolve().parent

# In-flight request checkpoint: written right after a successful submit so a
# crashed or interrupted run resumes polling the SAME request instead of
# resubmitting the PDF and spending credits twice. Deleted once output.json
# is safely on disk; only --force-extract ignores it.
_CHECKPOINT_NAME = ".datalab-request.json"

# Files that are pipeline by-products, not extraction artifacts: kept out of
# the Drive copy so the offsite folder stays byte-comparable to what Datalab
# produced (chunks are regenerated from output.json in seconds).
_DRIVE_EXCLUDES = ("chunks.ndjson", _CHECKPOINT_NAME)


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def die(msg: str) -> "NoReturn":  # noqa: F821 (py3.9-safe annotation)
    log(f"error: {msg}")
    raise SystemExit(1)


def _api_key() -> str:
    key = os.environ.get("DATALAB_API_KEY", "")
    if not key:
        die("DATALAB_API_KEY is not set (get one at https://www.datalab.to)")
    return key


def _multipart(fields: dict[str, str], file_field: str, pdf: Path) -> tuple[bytes, str]:
    """Encode form fields + one file as multipart/form-data (stdlib has none)."""
    boundary = f"----datalab-extract-{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{pdf.name}"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode()
    )
    parts.append(pdf.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _request_json(req: urllib.request.Request, timeout: int) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        die(f"HTTP {e.code} from {req.full_url}: {body}")


def _request_fingerprint(pdf: Path, mode: str, max_pages: int | None) -> dict:
    """What identifies 'the same request': file identity + paid parse params."""
    return {
        "pdf_name": pdf.name,
        "pdf_size": pdf.stat().st_size,
        "mode": mode,
        "max_pages": max_pages,
    }


def load_checkpoint(folder: Path, fingerprint: dict) -> str | None:
    """Return the in-flight check_url if a matching checkpoint exists."""
    path = folder / _CHECKPOINT_NAME
    if not path.is_file():
        return None
    try:
        ck = json.loads(path.read_text())
    except json.JSONDecodeError:
        log(f"warning: unreadable {path}; ignoring it")
        return None
    if ck.get("fingerprint") != fingerprint:
        log(
            "warning: checkpoint params differ from this invocation "
            f"({ck.get('fingerprint')} vs {fingerprint}); submitting fresh"
        )
        return None
    return ck.get("check_url")


def submit(pdf: Path, mode: str, max_pages: int | None, folder: Path) -> str:
    """Submit the conversion request; checkpoint and return request_check_url."""
    key = _api_key()  # fail fast, before the multipart body is built
    fields = {"output_format": "json", "mode": mode}
    if max_pages:
        fields["max_pages"] = str(max_pages)
    body, content_type = _multipart(fields, "file", pdf)
    log(f"submitting {pdf.name} ({pdf.stat().st_size / 1e6:.1f} MB, mode={mode}) ...")
    req = urllib.request.Request(
        DATALAB_CONVERT_URL,
        data=body,
        headers={"X-API-Key": key, "Content-Type": content_type},
        method="POST",
    )
    resp = _request_json(req, timeout=900)
    check_url = resp.get("request_check_url")
    if not check_url:
        die(f"no request_check_url in submit response: {json.dumps(resp)[:500]}")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / _CHECKPOINT_NAME).write_text(
        json.dumps(
            {
                "check_url": check_url,
                "fingerprint": _request_fingerprint(pdf, mode, max_pages),
                "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
        )
    )
    log(f"submitted; polling {check_url}")
    return check_url


def probe(check_url: str) -> dict | None:
    """One status GET; None if the request is gone server-side (HTTP 4xx).

    Any other failure dies with the checkpoint intact: when Datalab is
    unreachable or erroring we cannot know the request's fate, and
    resubmitting blind is exactly the double-spend this guard exists for.
    """
    req = urllib.request.Request(check_url, headers={"X-API-Key": _api_key()})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            return None
        die(f"HTTP {e.code} checking the in-flight request; retry later to resume")
    except (urllib.error.URLError, TimeoutError) as e:
        die(f"cannot reach Datalab to resume the in-flight request ({e}); retry later")


def poll(check_url: str, interval: int, deadline_s: int) -> dict:
    """Poll until the request leaves 'processing'; return the final envelope.

    Dies (checkpoint left in place) rather than resubmitting on timeout or
    persistent poll errors: the next run resumes this same request for free.
    """
    started = time.monotonic()
    failures = 0
    while True:
        req = urllib.request.Request(check_url, headers={"X-API-Key": _api_key()})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                env = json.load(resp)
            failures = 0
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            # Tolerate transient poll hiccups; the parse keeps running
            # server-side either way.
            failures += 1
            log(f"poll error ({failures}/5): {e}")
            if failures >= 5:
                die("5 consecutive poll failures; re-run to resume this request")
            time.sleep(interval)
            continue
        status = env.get("status")
        elapsed = int(time.monotonic() - started)
        if status == "processing":
            log(f"  processing ... ({elapsed}s)")
            if elapsed > deadline_s:
                die(
                    f"parse still processing after {deadline_s}s; "
                    "re-run later to resume this request"
                )
            time.sleep(interval)
            continue
        if status == "complete" and env.get("success"):
            return env
        die(
            f"parse ended status={status} success={env.get('success')} "
            f"error={env.get('error')!r}"
        )


def unwrap(env: dict, folder: Path) -> None:
    """Write the envelope out as the bare Marker folder layout."""
    doc = env.get("json") or {}
    if not doc.get("children"):
        die("envelope has no .json.children; nothing to write")
    folder.mkdir(parents=True, exist_ok=True)
    n_img = 0
    for name, b64 in (env.get("images") or {}).items():
        (folder / os.path.basename(name)).write_bytes(base64.b64decode(b64))
        n_img += 1
    (folder / "output.metadata.json").write_text(json.dumps(env.get("metadata") or {}))
    (folder / "output.json").write_text(json.dumps(doc, ensure_ascii=False))
    quality = env.get("parse_quality_score")
    cost = env.get("total_cost")
    log(
        f"extracted: {env.get('page_count')} pages, {n_img} images, "
        f"quality={quality}, cost={cost}"
    )


def drive_copy(folder: Path, remote: str, root_folder_id: str) -> None:
    if not shutil.which("rclone"):
        die("rclone not found on PATH")
    cmd = [
        "rclone",
        "copy",
        str(folder),
        f"{remote}:{folder.name}",
        "--drive-root-folder-id",
        root_folder_id,
        "--transfers",
        "8",
    ]
    for pattern in _DRIVE_EXCLUDES:
        cmd += ["--exclude", pattern]
    log(f"copying to Drive ({remote}:{folder.name}) ...")
    subprocess.run(cmd, check=True, timeout=3600)
    log("drive copy done (offsite)")


def build_chunks(folder: Path, book_id: str) -> None:
    marker = _TOOLS_DIR.parent / "marker.py"
    subprocess.run(
        [
            sys.executable,
            str(marker),
            str(folder / "output.json"),
            "--book-id",
            book_id,
            "-o",
            str(folder / "chunks.ndjson"),
        ],
        check=True,
        timeout=600,
    )


def upload_s3(folder: Path, book_id: str) -> None:
    subprocess.run(
        ["bash", str(_TOOLS_DIR / "upload-book.sh"), book_id, str(folder)],
        check=True,
        timeout=3600,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("book_id", help="kebab-case book id (S3 path + Drive folder)")
    ap.add_argument("pdf", help="path to the source PDF")
    ap.add_argument(
        "--mode",
        default="accurate",
        choices=["fast", "balanced", "accurate"],
        help="Datalab parse mode (default: accurate, matches existing corpus)",
    )
    ap.add_argument("--max-pages", type=int, help="limit pages (smoke tests)")
    ap.add_argument(
        "--out-dir",
        default=".",
        help="parent dir for the local <book-id>/ folder (default: cwd)",
    )
    ap.add_argument("--drive-remote", default=DEFAULT_DRIVE_REMOTE)
    ap.add_argument("--drive-folder-id", default=DEFAULT_DRIVE_FOLDER_ID)
    ap.add_argument("--poll-interval", type=int, default=15, help="seconds")
    ap.add_argument(
        "--timeout",
        type=int,
        default=7200,
        help="max seconds to wait for the parse (default: 7200)",
    )
    ap.add_argument(
        "--force-extract",
        action="store_true",
        help="call the API even if output.json already exists locally",
    )
    ap.add_argument("--skip-drive", action="store_true", help="skip Drive copy")
    ap.add_argument("--skip-s3", action="store_true", help="skip S3 upload")
    args = ap.parse_args(argv)

    if not _BOOK_ID_RE.match(args.book_id):
        die(f"book id {args.book_id!r} is not kebab-case ([a-z0-9-])")
    pdf = Path(args.pdf)
    if not pdf.is_file():
        die(f"PDF not found: {pdf}")
    folder = Path(args.out_dir) / args.book_id

    if (folder / "output.json").is_file() and not args.force_extract:
        log(f"{folder}/output.json exists; skipping API call (--force-extract to redo)")
    else:
        fingerprint = _request_fingerprint(pdf, args.mode, args.max_pages)
        check_url = None if args.force_extract else load_checkpoint(folder, fingerprint)
        env = None
        if check_url:
            log(f"resuming in-flight request from checkpoint: {check_url}")
            env = probe(check_url)
            if env is None:
                log("warning: checkpointed request is gone server-side; resubmitting")
                check_url = None
            elif env.get("status") == "complete" and env.get("success"):
                pass  # finished while we were away; no polling needed
            elif env.get("status") == "processing":
                env = None  # still running; fall through to the poll loop
            else:
                die(
                    f"checkpointed request ended status={env.get('status')} "
                    f"error={env.get('error')!r}; --force-extract to resubmit"
                )
        if check_url is None:
            check_url = submit(pdf, args.mode, args.max_pages, folder)
        if env is None:
            env = poll(check_url, args.poll_interval, args.timeout)
        unwrap(env, folder)
        (folder / _CHECKPOINT_NAME).unlink(missing_ok=True)

    source = folder / "source.pdf"
    if not source.is_file():
        shutil.copyfile(pdf, source)

    if args.skip_drive:
        log("skipping Drive copy (--skip-drive)")
    else:
        drive_copy(folder, args.drive_remote, args.drive_folder_id)

    build_chunks(folder, args.book_id)

    if args.skip_s3:
        log("skipping S3 upload (--skip-s3)")
    else:
        upload_s3(folder, args.book_id)

    log(
        f"done: {args.book_id}. Chunks load at the daily grimoire-load-chunks "
        "run (02:00 UTC); entity extraction is a manual Workflow submit."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
