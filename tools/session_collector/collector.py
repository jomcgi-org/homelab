"""Session discovery, filtering, rendering, and upload orchestration."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from tools.cli.auth import read_cached_cf_token

from . import claude_v1, codex_v1
from .models import Session
from .render import RenderedSession, render
from .scope import allowed_scope, discover_repo, reset_worktree_cache
from .state import eligible, load, locked, save
from .upload import upload_raw

MIN_BODY_BYTES = 2 * 1024
TOKEN_MESSAGE = "token expired: run cloudflared access login https://private.jomcgi.dev"


def parse_session(path: Path) -> Session:
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                return (
                    codex_v1.parse(path)
                    if isinstance(record.get("payload"), dict)
                    else claude_v1.parse(path)
                )
    return claude_v1.parse(path)


def _body_size(markdown: str) -> int:
    parts = markdown.split("---\n", 2)
    body = parts[2] if len(parts) == 3 else markdown
    return len(body.encode("utf-8"))


def _entry(
    path: Path,
    session: Session,
    repo: str | None,
    status: str,
    reason: str | None,
    raw_id: str | None,
    failures: int = 0,
) -> dict[str, object]:
    stat = path.stat()
    return {
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "raw_id": raw_id,
        "status": status,
        "reason": reason,
        "failures": failures,
        "cwd": session.cwd,
        "repo": repo,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }


def _payload(
    session: Session, rendered: RenderedSession, file_size: int
) -> dict[str, object]:
    metadata = rendered.metadata
    return {
        "content": rendered.markdown,
        "source": f"{session.provider}-session",
        "original_url": f"{session.provider}-session:{metadata['session_id']}",
        "extra": {
            "session_id": metadata["session_id"],
            "provider": metadata["provider"],
            "cwd": metadata["cwd"],
            "repo": metadata["repo"],
            "scope": metadata["scope"],
            "git_branch": metadata["git_branch"],
            "model": metadata["model"],
            "started_at": metadata["started_at"],
            "ended_at": metadata["ended_at"],
            "records_total": metadata["records_total"],
            "records_kept": metadata["records_kept"],
            "redactions": rendered.redactions,
            "bytes_original": file_size,
            "truncated": rendered.truncated,
            "collector_version": metadata["collector_version"],
        },
    }


def discover(claude_dir: Path, codex_dir: Path) -> list[Path]:
    paths = list(claude_dir.expanduser().glob("*/*.jsonl"))
    paths.extend(codex_dir.expanduser().glob("*/*/*/*.jsonl"))
    return sorted(path.resolve() for path in paths if path.is_file())


def run_collection(
    *,
    claude_dir: Path,
    codex_dir: Path,
    state_file: Path,
    allowlist: dict[str, str],
    path_allowlist: dict[Path, str] | None = None,
    quiet_minutes: float = 30,
    max_uploads: int = 20,
    base_url: str = "https://private.jomcgi.dev",
    dry_run: bool = False,
    client: httpx.Client | None = None,
    token_reader: Callable[[str], str | None] | None = None,
    now: float | None = None,
) -> int:
    state_file = state_file.expanduser()
    reset_worktree_cache()
    with locked(state_file):
        return _run_collection_locked(
            claude_dir=claude_dir,
            codex_dir=codex_dir,
            state_file=state_file,
            allowlist=allowlist,
            path_allowlist=path_allowlist,
            quiet_minutes=quiet_minutes,
            max_uploads=max_uploads,
            base_url=base_url,
            dry_run=dry_run,
            client=client,
            token_reader=token_reader,
            now=now,
        )


def _run_collection_locked(
    *,
    claude_dir: Path,
    codex_dir: Path,
    state_file: Path,
    allowlist: dict[str, str],
    path_allowlist: dict[Path, str] | None,
    quiet_minutes: float,
    max_uploads: int,
    base_url: str,
    dry_run: bool,
    client: httpx.Client | None,
    token_reader: Callable[[str], str | None] | None,
    now: float | None,
) -> int:
    state_value = load(state_file)
    candidates = [
        path
        for path in discover(claude_dir, codex_dir)
        if eligible(path, state_value.get(str(path)), quiet_minutes, now)
    ]
    token: str | None = None
    token_checked = False
    owned_client = client is None
    if client is None:
        client = httpx.Client(timeout=60, follow_redirects=False)

    attempted = 0
    try:
        for path in candidates:
            if attempted >= max_uploads:
                break
            key = str(path)
            session: Session | None = None
            repo: str | None = None
            try:
                session = parse_session(path)
                repo = discover_repo(
                    session.cwd, state_value, path_allowlist, session.git_origin
                )
                scope = allowed_scope(repo, allowlist)
                if scope is None:
                    if not dry_run:
                        state_value[key] = _entry(
                            path, session, repo, "skipped", "outside allowlist", None
                        )
                        save(state_file, state_value)
                    print(f"skipped {path}: outside allowlist", file=sys.stderr)
                    continue
                rendered = render(session, repo, scope)
                if _body_size(rendered.markdown) < MIN_BODY_BYTES:
                    if not dry_run:
                        state_value[key] = _entry(
                            path, session, repo, "skipped", "empty", None
                        )
                        save(state_file, state_value)
                    print(f"skipped {path}: empty", file=sys.stderr)
                    continue
                attempted += 1
                if dry_run:
                    print(f"would upload {path} redactions={rendered.redactions}")
                    continue
                if not token_checked:
                    hostname = urlparse(base_url).hostname or "private.jomcgi.dev"
                    reader = token_reader or read_cached_cf_token
                    token = reader(hostname)
                    token_checked = True
                    if not token:
                        print(TOKEN_MESSAGE, file=sys.stderr)
                        return 0
                result = upload_raw(
                    client,
                    base_url,
                    token or "",
                    _payload(session, rendered, path.stat().st_size),
                )
                if result.status == "expired":
                    print(TOKEN_MESSAGE, file=sys.stderr)
                    return 0
                if result.status == "uploaded":
                    state_value[key] = _entry(
                        path, session, repo, "uploaded", None, result.raw_id
                    )
                    print(f"uploaded {path}: {result.raw_id}", file=sys.stderr)
                else:
                    reason = f"HTTP {result.status_code}"
                    state_value[key] = _entry(
                        path,
                        session,
                        repo,
                        "failed",
                        reason,
                        None,
                        _next_failure_count(path, state_value.get(key)),
                    )
                    print(f"failed {path}: {reason}", file=sys.stderr)
                save(state_file, state_value)
            except Exception as error:
                reason = type(error).__name__
                try:
                    stat = path.stat()
                except OSError:
                    print(f"failed {path}: {reason}", file=sys.stderr)
                    continue
                state_value[key] = {
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "raw_id": None,
                    "status": "failed",
                    "reason": reason,
                    "failures": _next_failure_count(path, state_value.get(key)),
                    "cwd": session.cwd if session is not None else "",
                    "repo": repo,
                    "uploaded_at": datetime.now(timezone.utc).isoformat(),
                }
                save(state_file, state_value)
                print(f"failed {path}: {reason}", file=sys.stderr)
    finally:
        if owned_client:
            client.close()
    return 0


def _next_failure_count(path: Path, entry: dict[str, object] | None) -> int:
    if not entry or path.stat().st_size > int(entry.get("size", -1)):
        return 1
    return int(entry.get("failures", 0)) + 1
