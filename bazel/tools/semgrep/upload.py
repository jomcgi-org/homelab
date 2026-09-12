"""Best-effort upload of validated Semgrep results to Semgrep App.

The scan wrappers enforce credentials before scanning. This helper therefore
never decides whether a scan may run and never changes the local scan result.
Every remote operation is bounded and failures are reported without raising.

Usage: upload.py <results-json-path> <scan-exit-code>

Environment:
    SEMGREP_APP_TOKEN     required by the scan wrapper
    SEMGREP_URL           optional, defaults to https://semgrep.dev
    SEMGREP_REPO          optional repository identity
    GITHUB_REPOSITORY     repository identity fallback
    GITHUB_SHA/GIT_COMMIT commit identity
    GITHUB_REF_NAME/GIT_BRANCH branch identity
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from pathlib import Path

import httpx

TIMEOUT = httpx.Timeout(10.0, connect=5.0)
LOGGER = logging.getLogger(__name__)


def _environment_value(*names: str, default: str = "unknown") -> str:
    """Return the first non-empty metadata value from the environment."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _detect_repo() -> str:
    """Return repository metadata without crossing the Bazel sandbox."""
    return _environment_value(
        "SEMGREP_REPO", "GITHUB_REPOSITORY", default="unknown/unknown"
    )


def _detect_commit() -> str:
    """Return commit metadata without invoking git."""
    return _environment_value("GITHUB_SHA", "GIT_COMMIT")


def _detect_branch() -> str:
    """Return branch metadata without invoking git."""
    return _environment_value("GITHUB_REF_NAME", "GIT_BRANCH")


def _detect_semgrep_version() -> str:
    """Return the version exported from the engine that produced the results."""
    return _environment_value("SEMGREP_ENGINE_VERSION")


def _warn(stage: str, error: Exception) -> None:
    detail = str(error)
    token = os.environ.get("SEMGREP_APP_TOKEN", "")
    if token:
        detail = detail.replace(token, "[REDACTED]")
    message = f"upload.py: {stage} failed (non-fatal): {detail}"
    LOGGER.warning("%s", message)
    print(message, file=sys.stderr)


def upload(results_path: Path, scan_exit_code: int) -> None:
    """Run the Semgrep App lifecycle, preserving the local scan outcome."""
    token = os.environ.get("SEMGREP_APP_TOKEN", "")
    if not token.strip():
        _warn("configuration", ValueError("SEMGREP_APP_TOKEN is empty"))
        return

    try:
        results = json.loads(results_path.read_text())
    except (OSError, ValueError) as error:
        _warn("reading results", error)
        return

    base_url = os.environ.get("SEMGREP_URL", "https://semgrep.dev").rstrip("/")
    version = _detect_semgrep_version()
    repo = _detect_repo()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    registration = {
        "scan_metadata": {
            "cli_version": version,
            "unique_id": str(uuid.uuid4()),
            "requested_products": ["sast"],
            "dry_run": False,
        },
        "project_metadata": {
            "semgrep_version": version,
            "repository": repo,
            "repo_url": f"https://github.com/{repo}",
            "branch": _detect_branch(),
            "commit": _detect_commit(),
            "is_full_scan": True,
        },
    }

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            try:
                response = client.post(
                    f"{base_url}/api/cli/scans", headers=headers, json=registration
                )
                response.raise_for_status()
                scan_id = response.json()["info"]["id"]
                if not isinstance(scan_id, (str, int)) or not str(scan_id).strip():
                    raise ValueError("registration response has no scan id")
            except Exception as error:  # noqa: BLE001 - App failures are best-effort.
                _warn("registration", error)
                return

            try:
                client.post(
                    f"{base_url}/api/agent/scans/{scan_id}/results",
                    headers=headers,
                    json=results,
                ).raise_for_status()
            except Exception as error:  # noqa: BLE001 - App failures are best-effort.
                _warn("findings upload", error)

            try:
                client.post(
                    f"{base_url}/api/agent/scans/{scan_id}/complete",
                    headers=headers,
                    json={"exit_code": scan_exit_code},
                ).raise_for_status()
            except Exception as error:  # noqa: BLE001 - App failures are best-effort.
                _warn("completion", error)
                return

            print(f"upload.py: completed Semgrep App scan {scan_id}", file=sys.stderr)
    except Exception as error:  # noqa: BLE001 - App failures are best-effort.
        _warn("client setup", error)


def main() -> None:
    if len(sys.argv) != 3:
        print("upload.py: expected <results-path> <exit-code>", file=sys.stderr)
        return
    try:
        scan_exit_code = int(sys.argv[2])
    except ValueError as error:
        _warn("reading exit code", error)
        return
    upload(Path(sys.argv[1]), scan_exit_code)


if __name__ == "__main__":
    main()
