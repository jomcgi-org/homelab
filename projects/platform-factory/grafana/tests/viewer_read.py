"""Exercise a real MCP exchange against the freshly deployed server."""

import asyncio
import os

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def check():
    url = "http://127.0.0.1:8000/mcp"
    headers = {"Host": "mcp-grafana.grafana.svc.cluster.local:8000"}
    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json={})
        assert response.status_code == 401, response.status_code

    headers["Authorization"] = "Bearer " + os.environ["MCP_GRAFANA_SERVER_TOKEN"]
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("list_datasources", {})
            assert not result.isError, "Viewer datasource read failed"
            assert "Prometheus" in result.model_dump_json()


if __name__ == "__main__":
    asyncio.run(check())
