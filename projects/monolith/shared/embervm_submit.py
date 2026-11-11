"""HTTP plumbing for synchronous EmberVM task submission."""

from __future__ import annotations

import httpx

_UNSET = object()


async def submit_task(
    *,
    base_url: str,
    workload: str,
    timeout: httpx.Timeout,
    headers: dict[str, str],
    content: bytes | None = None,
    json: object = _UNSET,
) -> httpx.Response:
    """POST exactly one raw or JSON payload and return the unmodified response."""
    if (content is None) == (json is _UNSET):
        raise ValueError("provide exactly one of content or json")
    url = f"{base_url}/v1/workloads/{workload}/tasks?wait=true"
    payload = {"content": content} if content is not None else {"json": json}
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(url, headers=headers, **payload)
