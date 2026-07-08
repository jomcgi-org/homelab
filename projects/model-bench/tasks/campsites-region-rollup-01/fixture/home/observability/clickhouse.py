from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class ClickHouseClient:
    """Minimal async ClickHouse HTTP client."""

    def __init__(
        self,
        base_url: str = "",
        user: str = "",
        password: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ):
        kwargs: dict = {"base_url": base_url}
        if user and password:
            kwargs["auth"] = httpx.BasicAuth(user, password)
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.AsyncClient(timeout=timeout, **kwargs)

    async def _query(self, sql: str) -> dict:
        query = sql.rstrip().rstrip(";")
        if not query.upper().endswith("FORMAT JSON"):
            query += "\nFORMAT JSON"
        resp = await self._client.post("/", content=query)
        resp.raise_for_status()
        return resp.json()

    async def query_scalar(self, sql: str) -> float | None:
        """Execute query and return 'value' from first row, or None."""
        result = await self._query(sql)
        if not result.get("data"):
            return None
        return result["data"][0].get("value")

    async def query_rows(self, sql: str) -> list[dict]:
        """Execute query and return all rows."""
        result = await self._query(sql)
        return result.get("data", [])

    async def close(self):
        await self._client.aclose()
