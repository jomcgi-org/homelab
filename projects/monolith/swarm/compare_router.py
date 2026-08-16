from __future__ import annotations

import asyncio
import json
import os
import time
import zlib
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Query

from agent_sessions.rationale import parse_rationale
from swarm.unified_diff import parse_unified_diff

router = APIRouter(prefix="/compare", tags=["swarm"])

_GITHUB_API = "https://api.github.com"
_DEFAULT_REPO = "jomcgi/homelab"
_COMPARE_BASE = "main"
_CACHE_TTL_S = 90.0
_cache: dict[str, tuple[float, dict]] = {}


def _decode(value: str | None, default):
    if not value:
        return default
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
    return decoded


def _turn_data(session_id: int, turn_seq: int):
    from agent_sessions import store
    from core.db import get_engine
    from sqlmodel import Session

    with Session(get_engine()) as db:
        turn = store.get_turn(db, session_id, turn_seq)
        session = store.get_session(db, session_id)
        if turn is None or session is None:
            return None
        return {
            "base_sha": turn.base_sha,
            "commit_sha": turn.commit_sha,
            "diff_blob": turn.diff_blob,
            "diff_truncated": turn.diff_truncated,
            "diff_base_sha": turn.diff_base_sha,
            "branch": session.branch,
            "repo": session.repo or _DEFAULT_REPO,
            "usage_json": turn.usage_json,
            "prompt_intent": turn.prompt_intent,
            "result_text": turn.result_text,
            "rationale": parse_rationale(turn.result_text),
        }


def _stored_compare(data: dict) -> dict | None:
    blob = data.get("diff_blob")
    if blob is None:
        return None
    try:
        decompressor = zlib.decompressobj()
        raw_bytes = decompressor.decompress(bytes(blob), 5 * 1024 * 1024 + 1)
        if (
            len(raw_bytes) > 5 * 1024 * 1024
            or not decompressor.eof
            or decompressor.unused_data
        ):
            return None
        raw = raw_bytes.decode("utf-8", "replace")
    except (TypeError, ValueError, zlib.error):
        return None
    return {
        "files": parse_unified_diff(raw),
        "truncated": False,
        "source": "stored",
    }


def _activities(data: dict) -> tuple[set[str], bool]:
    usage = _decode(data.get("usage_json"), {})
    activities = usage.get("activities", []) if isinstance(usage, dict) else []
    if not isinstance(activities, list):
        return set(), False
    paths = {
        item.get("file_path")
        for item in activities
        if isinstance(item, dict)
        and item.get("type") in {"edit", "write"}
        and isinstance(item.get("file_path"), str)
    }
    return paths, len(activities) >= 300


def _cache_get(key: str) -> dict | None:
    entry = _cache.get(key)
    if entry is None or time.monotonic() - entry[0] >= _CACHE_TTL_S:
        return None
    return entry[1]


def _cache_put(key: str, value: dict) -> None:
    _cache[key] = (time.monotonic(), value)


async def _github_get(url: str) -> httpx.Response:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, headers=headers)
    return response


async def _compare(
    repo: str, base: str, head: str, cache_key: str, *, include_patches: bool = False
) -> dict:
    if not include_patches:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
    response = await _github_get(
        f"{_GITHUB_API}/repos/{repo}/compare/{quote(base, safe='')}...{quote(head, safe='')}"
    )
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="GitHub compare unavailable")
    payload = response.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Invalid GitHub compare response")
    files = payload.get("files", [])
    if not isinstance(files, list):
        files = []
    result = {
        "files": [
            {
                "path": item.get("filename"),
                "status": item.get("status"),
                "additions": item.get("additions", 0),
                "deletions": item.get("deletions", 0),
                "changes": item.get("changes", 0),
                "patch": item.get("patch") if include_patches else None,
            }
            for item in files
            if isinstance(item, dict) and isinstance(item.get("filename"), str)
        ],
        "truncated": bool(payload.get("truncated")),
    }
    if not include_patches:
        _cache_put(cache_key, result)
    return result


def _public_files(compare: dict, authored: set[str], session_id: int, turn_seq: int):
    return [
        {
            key: item[key]
            for key in ("path", "status", "additions", "deletions", "changes")
        }
        | {
            "classification": "authored" if item["path"] in authored else "mechanical",
            "patch_url": f"/api/swarm/compare/{session_id}/{turn_seq}/patch?path={quote(item['path'], safe='')}"
            if item["path"] in authored
            else None,
        }
        for item in compare["files"]
    ]


@router.get("/{session_id}/{turn_seq}")
async def compare_stats(session_id: int, turn_seq: int) -> dict:
    data = await asyncio.to_thread(_turn_data, session_id, turn_seq)
    if data is None:
        raise HTTPException(status_code=404, detail="Agent turn not found")
    base_sha, commit_sha, branch = data["base_sha"], data["commit_sha"], data["branch"]
    repo = data["repo"]
    observed, activities_truncated = _activities(data)
    rationale = parse_rationale(data["result_text"])
    trailer_paths = {item["path"] for item in rationale.get("paths", [])}
    # A file is accounted for if the activity stream shows it being edited OR
    # the agent's trailer names it. Observation alone is not enough: several
    # adapters record no edit/write activities at all, so `observed` is empty
    # for their turns and every changed file fell through as mechanical. With
    # no `run` activities either, _mechanical_steps also returns nothing, so a
    # turn with a real diff and a parsed trailer produced zero steps and the
    # console rendered an empty panel beside correct counts.
    #
    # This does not merge the two channels. The diff still comes from git and
    # the testimony still from the agent; the composer attaches testimony only
    # where the trailer actually has a point. What changes is that the claim no
    # longer needs a third signal to corroborate it before it can be shown.
    # `authored_file_paths` below stays the OBSERVED set, so the distinction
    # between seen and claimed is still reportable.
    authored = observed | trailer_paths

    resolution_rung = 3
    diff_type = None
    compare = {"files": [], "truncated": False}
    stored = _stored_compare(data)
    if stored is not None:
        resolution_rung, diff_type = 1, "stored"
        compare = stored
        base_sha = data.get("diff_base_sha") or base_sha
    elif base_sha and commit_sha:
        resolution_rung, diff_type = 1, "sha"
        compare = await _compare(
            repo, base_sha, commit_sha, f"{base_sha}...{commit_sha}"
        )
    elif branch == _COMPARE_BASE:
        # A base branch compared with itself is not evidence of an empty
        # change. Leave the diff unresolved so testimony is not contradicted
        # by an artifact that was never available.
        resolution_rung, diff_type = 3, None
    elif branch:
        branch_response = await _github_get(
            f"{_GITHUB_API}/repos/{repo}/branches/{quote(branch, safe='')}"
        )
        if branch_response.status_code == 200:
            resolution_rung, diff_type = 2, "branch_ephemeral"
            compare = await _compare(repo, _COMPARE_BASE, branch, f"branch:{branch}")

    paths = {item["path"] for item in compare["files"]}
    # The cross-check is only meaningful against a diff we actually resolved.
    # At rung 3 there is no compare, so `paths` is empty because nothing was
    # fetched, not because the agent changed nothing. Differencing against it
    # would report every path the agent named as contradicted and every
    # changed file as explained, which states the opposite of the truth in
    # both directions (issue #4817). Absence of evidence is not a finding:
    # omit both halves and let `cross_checked` say the check did not run.
    cross_checked = resolution_rung in (1, 2)
    result = {
        "resolution_rung": resolution_rung,
        "cross_checked": cross_checked,
        "diff_type": diff_type,
        "base_sha": base_sha,
        "commit_sha": commit_sha,
        "branch": branch,
        "files": _public_files(compare, authored, session_id, turn_seq),
        "stats": {
            "total_files": len(compare["files"]),
            **(
                {
                    "truncated_at": 300,
                    "truncated_reason": "GitHub files array capped at 300",
                }
                if compare["truncated"]
                else {}
            ),
        },
        "trailer_parsed": rationale.get("parse_status") == "parsed",
        "authored_file_paths": sorted(observed),
        **(
            {
                "unexplained_files": sorted(paths - trailer_paths),
                "contradicted_paths": sorted(trailer_paths - paths),
            }
            if cross_checked
            else {}
        ),
    }
    if activities_truncated:
        result["activities_truncated"] = True
        result["activities_truncated_reason"] = "activities ingest capped at 300"
    if resolution_rung == 3:
        result["error"] = "no_compare_available"
    return result


@router.get("/{session_id}/{turn_seq}/patch")
async def compare_patch(session_id: int, turn_seq: int, path: str = Query(...)) -> dict:
    data = await asyncio.to_thread(_turn_data, session_id, turn_seq)
    if data is None:
        raise HTTPException(status_code=404, detail="Agent turn not found")
    stored = _stored_compare(data)
    if stored is not None:
        compare = stored
    elif data["base_sha"] and data["commit_sha"]:
        compare = await _compare(
            data["repo"],
            data["base_sha"],
            data["commit_sha"],
            f"{data['base_sha']}...{data['commit_sha']}",
            include_patches=True,
        )
    elif data["branch"] == _COMPARE_BASE:
        raise HTTPException(status_code=404, detail="no_compare_available")
    elif data["branch"]:
        branch_response = await _github_get(
            f"{_GITHUB_API}/repos/{data['repo']}/branches/{quote(data['branch'], safe='')}"
        )
        if branch_response.status_code != 200:
            raise HTTPException(status_code=404, detail="no_compare_available")
        compare = await _compare(
            data["repo"],
            _COMPARE_BASE,
            data["branch"],
            f"branch:{data['branch']}",
            include_patches=True,
        )
    else:
        raise HTTPException(status_code=404, detail="no_compare_available")
    item = next((item for item in compare["files"] if item["path"] == path), None)
    if item is None:
        raise HTTPException(status_code=404, detail="file not found in compare")
    return {"path": path, "patch": item["patch"]}
