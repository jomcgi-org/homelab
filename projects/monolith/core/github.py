"""Shared GitHub repository configuration and lightweight REST client.

The repository moved from ``jomcgi/homelab`` to ``jomcgi-org/homelab`` on
2026-08-22. GitHub returns a 301 redirect for the old path, and httpx does not
follow redirects by default.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

GITHUB_API = "https://api.github.com"
GITHUB_REPO = os.environ.get("GITHUB_REPO", "jomcgi-org/homelab")

logger = logging.getLogger(__name__)

_MAX_PULL_PAGES = 100
_MERGED_PULL_QUERY = """
query MergedPullRequests($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(
      first: 100
      after: $cursor
      states: MERGED
      orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      nodes {
        number
        title
        mergedAt
        updatedAt
        additions
        deletions
        changedFiles
        body
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "monolith-merged-pr-snapshot",
    }
    token = os.environ.get("GITHUB_API_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _parse_github_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fetch_merged_pull_requests(
    cutoff: datetime,
    *,
    watermark: datetime | None = None,
    repo: str | None = None,
    client: httpx.Client | None = None,
) -> list[dict]:
    """Fetch merged pull requests whose merge time is at or after ``cutoff``.

    A GraphQL page includes the aggregate diff statistics that the REST list
    omits. Pulls are ordered by ``updatedAt``. Initial snapshots page through
    the requested window, while refreshes stop after crossing the prior
    snapshot watermark with a one-day overlap.
    """
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    else:
        cutoff = cutoff.astimezone(timezone.utc)
    if watermark is None:
        pagination_cutoff = cutoff
    elif watermark.tzinfo is None:
        pagination_cutoff = watermark.replace(tzinfo=timezone.utc) - timedelta(days=1)
    else:
        pagination_cutoff = watermark.astimezone(timezone.utc) - timedelta(days=1)

    repository = repo or GITHUB_REPO
    try:
        owner, name = repository.split("/", 1)
    except ValueError as exc:
        raise ValueError("GitHub repository must use owner/name form") from exc
    owned_client = client is None
    if client is None:
        client = httpx.Client(timeout=20.0, follow_redirects=True)

    merged: list[dict] = []
    try:
        cursor = None
        for page in range(1, _MAX_PULL_PAGES + 1):
            response = client.post(
                f"{GITHUB_API}/graphql",
                headers=_github_headers(),
                json={
                    "query": _MERGED_PULL_QUERY,
                    "variables": {
                        "owner": owner,
                        "name": name,
                        "cursor": cursor,
                    },
                },
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("errors"):
                raise RuntimeError(f"GitHub GraphQL error: {payload['errors']}")
            try:
                connection = payload["data"]["repository"]["pullRequests"]
                batch = connection["nodes"]
                page_info = connection["pageInfo"]
            except (KeyError, TypeError) as exc:
                raise ValueError(
                    "GitHub pull response had an unexpected shape"
                ) from exc
            if not isinstance(batch, list):
                raise ValueError("GitHub pull response nodes was not a list")
            if not batch:
                break

            updated_times = []
            for pull in batch:
                if not isinstance(pull, dict):
                    continue
                updated_at = _parse_github_datetime(pull.get("updatedAt"))
                if updated_at is not None:
                    updated_times.append(updated_at)

                merged_at = _parse_github_datetime(pull.get("mergedAt"))
                if merged_at is None or merged_at < cutoff:
                    continue

                merged.append(
                    {
                        "number": pull.get("number"),
                        "title": pull.get("title"),
                        "merged_at": pull.get("mergedAt"),
                        "additions": pull.get("additions"),
                        "deletions": pull.get("deletions"),
                        "changed_files": pull.get("changedFiles"),
                        "body": pull.get("body"),
                    }
                )

            oldest_updated_at = min(updated_times, default=None)
            crossed_pagination_cutoff = (
                oldest_updated_at is not None and oldest_updated_at < pagination_cutoff
            )
            has_next_page = bool(page_info.get("hasNextPage"))
            if crossed_pagination_cutoff or not has_next_page:
                break
            if page == _MAX_PULL_PAGES:
                logger.warning(
                    "GitHub merged pull request pagination hit the %d-page cap",
                    _MAX_PULL_PAGES,
                )
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                raise ValueError("GitHub pull response omitted the next cursor")
    finally:
        if owned_client:
            client.close()

    return merged
