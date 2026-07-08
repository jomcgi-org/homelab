"""Web search via SearXNG -- used as a PydanticAI tool."""

import os

import httpx

SEARXNG_URL = os.environ.get("SEARXNG_URL", "")


async def search_web(query: str, base_url: str | None = None) -> str:
    """Search the web via SearXNG, returning top 5 results as text."""
    url = base_url or SEARXNG_URL
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(15.0),
        headers={"X-Forwarded-For": "127.0.0.1"},
    ) as client:
        resp = await client.get(
            f"{url}/search",
            params={"q": query, "format": "json"},
        )
        resp.raise_for_status()
        try:
            results = resp.json()["results"][:5]
        except (KeyError, TypeError) as e:
            raise ValueError(f"unexpected search response shape: {e}") from e
        return "\n\n".join(
            f"**{r['title']}**\n{r['content']}\nURL: {r['url']}" for r in results
        )
