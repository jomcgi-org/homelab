"""HTTP upload helpers."""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass
class UploadResult:
    status: str
    raw_id: str | None = None
    created: bool | None = None
    status_code: int | None = None


def _headers(token: str | None, cloudflare: bool) -> dict[str, str] | None:
    return {"Cookie": f"CF_Authorization={token}"} if cloudflare else None


def upload_raw(
    client: httpx.Client,
    base_url: str,
    token: str | None,
    payload: dict[str, object],
    *,
    cloudflare: bool,
) -> UploadResult:
    response = client.post(
        f"{base_url.rstrip('/')}/api/knowledge/raws",
        json=payload,
        headers=_headers(token, cloudflare),
    )
    if response.status_code in {200, 201}:
        try:
            data = response.json()
            raw_id = str(data["raw_id"])
        except (KeyError, TypeError, ValueError):
            return UploadResult("failed", status_code=response.status_code)
        return UploadResult(
            "uploaded", raw_id, bool(data.get("created")), response.status_code
        )
    if cloudflare and (
        response.status_code in {401, 403} or 300 <= response.status_code < 400
    ):
        return UploadResult("expired", status_code=response.status_code)
    return UploadResult("failed", status_code=response.status_code)


def upload_usage(
    client: httpx.Client,
    base_url: str,
    token: str | None,
    raw_id: str,
    payload: dict[str, object],
    *,
    cloudflare: bool,
) -> UploadResult:
    response = client.post(
        f"{base_url.rstrip('/')}/api/knowledge/raws/{raw_id}/usage",
        json=payload,
        headers=_headers(token, cloudflare),
    )
    if response.status_code == 200:
        return UploadResult("uploaded", raw_id, status_code=200)
    if cloudflare and (
        response.status_code in {401, 403} or 300 <= response.status_code < 400
    ):
        return UploadResult("expired", status_code=response.status_code)
    return UploadResult("failed", status_code=response.status_code)
