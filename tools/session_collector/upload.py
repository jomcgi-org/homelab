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


def upload_raw(
    client: httpx.Client, base_url: str, token: str, payload: dict[str, object]
) -> UploadResult:
    response = client.post(
        f"{base_url.rstrip('/')}/api/knowledge/raws",
        json=payload,
        headers={"Cookie": f"CF_Authorization={token}"},
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
    if response.status_code in {401, 403} or 300 <= response.status_code < 400:
        return UploadResult("expired", status_code=response.status_code)
    return UploadResult("failed", status_code=response.status_code)
