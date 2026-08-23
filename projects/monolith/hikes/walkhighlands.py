"""WalkHighlands scraper: pure HTML parsing plus a polite async fetch ladder.

Ported from projects/hikes/scrape_walkhighlands/scrape.py with fetch split
from parse: every parse_* function takes HTML and returns links or data
(unit-testable, no network), and fetch_all_walks drives the four-stage ladder
(homepage -> areas -> sub-areas -> walk pages) over a shared httpx client.

Selectors, the Walk shape, the TimeLength duration handling, and the
uuid5-of-coordinates identity are kept exactly as the original so re-scrapes
upsert cleanly onto the seeded corpus.
"""

import asyncio
import logging
import re
import uuid
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, ValidationError
from timelength import TimeLength

logger = logging.getLogger("monolith.hikes")

BASE_URL = "https://www.walkhighlands.co.uk/"
# Desktop User-Agent carried over from the original scraper.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36 Edg/116.0.1938.76"
)
HEADERS = {"User-Agent": USER_AGENT}
TIMEOUT_SECS = 15.0
# Be polite: this is someone else's site. Low concurrency plus a small delay
# per request (the delay sits inside the semaphore slot, so the overall
# request rate stays bounded).
CONCURRENCY = 3
POLITENESS_DELAY_SECS = 0.5
# The legacy scraper retried each fetch on transient failures; keep a small
# bounded retry with backoff so one flaky request does not drop a whole branch
# of the corpus (a failed homepage would otherwise abort the entire scrape).
MAX_FETCH_ATTEMPTS = 3
RETRY_BACKOFF_SECS = 1.0

# The original selector included an explicit tbody
# (div.walktable > table.table1 > tbody > tr > td:nth-child(1) > a). The
# stdlib html.parser does not synthesize tbody elements, so use a descendant
# combinator for the tr: it matches whether or not the source HTML carries a
# tbody.
_WALK_LINK_SELECTOR = "div.walktable > table.table1 tr > td:nth-child(1) > a"


class Walk(BaseModel):
    """A scraped walk. Identity is uuid5 of the coordinates, stable across scrapes."""

    uuid: str
    name: str
    url: str
    distance_km: float
    ascent_m: int
    duration_h: float
    summary: str
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)


def parse_area_links(html: str, base_url: str) -> list[str]:
    """Extract area page links from the homepage (#choosearea td.cell a).

    Skips .shtml and .php links (navigation chrome, not area pages), exactly
    as the original homepage logic did.
    """
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find("div", id="choosearea")
    if container is None:
        logger.warning("hikes scrape: no #choosearea container on homepage")
        return []

    links: list[str] = []
    for link in container.select("td.cell a"):
        href = link.get("href")
        name = link.get_text(strip=True)
        if not href or not name:
            continue
        absolute = urljoin(base_url, href)
        if ".shtml" in absolute or ".php" in absolute:
            continue
        links.append(absolute)
    return links


def parse_sub_area_links(html: str, base_url: str) -> list[str]:
    """Extract sub-area links from an area page (#arealist td.cell a, skip .php)."""
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find("div", id="arealist")
    if container is None:
        logger.warning("hikes scrape: no #arealist container on area page")
        return []

    links: list[str] = []
    for link in container.select("td.cell a"):
        href = link.get("href")
        name = link.get_text(strip=True)
        if not href or not name:
            continue
        absolute = urljoin(base_url, href)
        if ".php" in absolute:
            continue
        links.append(absolute)
    return links


def parse_walk_links(html: str, base_url: str) -> list[str]:
    """Extract walk detail links from a sub-area walk table (first cell per row)."""
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    for link in soup.select(_WALK_LINK_SELECTOR):
        href = link.get("href")
        name = link.get_text(strip=True)
        if not href or not name:
            continue
        links.append(urljoin(base_url, href))
    if not links:
        logger.warning("hikes scrape: no walk links found on sub-area page")
    return links


def _parse_duration_hours(text: str) -> float | None:
    """Parse a walk duration via TimeLength; ranges take the upper bound.

    "5.5 - 6.5 hours" parses the part after the dash (6.5), matching the original
    scraper. WalkHighlands also offers some long routes as both a single-push and
    a multi-day option, e.g. "18 hours/2 days" or "12 hours+ or 2 days". Passed
    whole, TimeLength SUMS every token (18h + 2 days = 66h), which is meaningless
    for the per-day doability model (it wants walking hours, not elapsed days).
    So when an hours alternative is present, keep only it and drop the "N days"
    framing. A pure "2 days" with no hours figure falls through to TimeLength
    unchanged (such routes are inherently multi-day).
    """
    segments = re.split(r"\s*/\s*|\s+or\s+", text)
    hour_segments = [seg for seg in segments if "hour" in seg.lower()]
    # "12 hours+" -> "12 hours": the trailing + would trip TimeLength.
    candidate = (hour_segments[0] if hour_segments else text).replace("+", " ").strip()
    try:
        if "-" in candidate:
            return TimeLength(candidate.split("-")[1]).to_hours()
        return TimeLength(candidate).to_hours()
    except Exception:
        logger.warning("hikes scrape: could not parse duration %r", text)
        return None


def parse_walk(html: str) -> Walk | None:
    """Extract one Walk from a walk detail page. Returns None on missing data."""
    soup = BeautifulSoup(html, "html.parser")
    data: dict = {}

    # The route title is an <h1 class="wtitle"> that now sits ABOVE #content
    # (WalkHighlands moved it out of the content div, and the page also carries
    # unrelated "app expired" <h1>s, so target the class). Fall back to the old
    # "#content h1" so a partial revert does not re-break parsing.
    name_tag = soup.select_one("h1.wtitle") or soup.select_one("#content h1")
    if name_tag:
        data["name"] = name_tag.get_text(strip=True)

    canonical = soup.select_one('link[rel="canonical"]')
    if canonical and canonical.get("href"):
        data["url"] = canonical["href"]

    # The heading titled "Summary" is followed by the summary paragraph. The page
    # now uses <h3> (was <h2>) and wraps the paragraph in a div rather than making
    # it a direct sibling, so match either heading level and take the next <p> in
    # document order (find_next), which works for both the old and new layouts.
    summary_heading = soup.find(["h2", "h3"], string=lambda t: t and "Summary" in t)
    summary_p = summary_heading.find_next("p") if summary_heading else None
    if summary_p:
        data["summary"] = summary_p.get_text(strip=True)

    # Walk statistics (distance, time, ascent) live in a dl inside #col.
    stats_dl = soup.select_one("#col dl")
    if stats_dl:
        for dt in stats_dl.find_all("dt"):
            dt_text = dt.get_text(strip=True).lower()
            dd = dt.find_next_sibling("dd")
            if not dd:
                continue
            dd_text = dd.get_text(strip=True)

            if "distance" in dt_text:
                match = re.search(r"([\d.]+)\s*km", dd_text)
                if match:
                    data["distance_km"] = float(match.group(1))
                else:
                    logger.warning("hikes scrape: no km value in distance %r", dd_text)
            elif "time" in dt_text:
                duration = _parse_duration_hours(dd_text)
                if duration is not None:
                    data["duration_h"] = duration
            elif "ascent" in dt_text:
                match = re.search(r"(\d+)\s*m", dd_text)
                if match:
                    data["ascent_m"] = int(match.group(1))
                else:
                    logger.warning("hikes scrape: no m value in ascent %r", dd_text)

    # Coordinates come from the "Open in Google Maps" link.
    location_tag = soup.select_one('a[href^="https://www.google.com/maps/search/"]')
    if location_tag:
        coords = re.findall(r"[-+]?\d*\.\d+|\d+", location_tag["href"])
        if len(coords) == 2:
            data["latitude"] = float(coords[0])
            data["longitude"] = float(coords[1])
        else:
            logger.warning(
                "hikes scrape: no coordinates in maps link %r", location_tag["href"]
            )

    if "latitude" not in data or "longitude" not in data:
        logger.warning(
            "hikes scrape: missing coordinates for %r, skipping", data.get("name")
        )
        return None

    # Walk identity: uuid5 of the coordinate pair, matching the seeded corpus.
    data["uuid"] = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"{data['latitude']},{data['longitude']}")
    )
    try:
        return Walk(**data)
    except ValidationError as exc:
        logger.warning(
            "hikes scrape: validation failed for %r: %s", data.get("name"), exc
        )
        return None


def _bump(stats: dict[str, int], key: str) -> None:
    stats[key] = stats.get(key, 0) + 1


async def _fetch(
    client: httpx.AsyncClient,
    url: str,
    semaphore: asyncio.Semaphore,
    stats: dict[str, int],
    stage: str,
) -> str | None:
    """Fetch one page politely, retrying transient failures.

    Failures after the final attempt are logged and counted, never raised.
    """
    async with semaphore:
        for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
            try:
                response = await client.get(url, headers=HEADERS, timeout=TIMEOUT_SECS)
                response.raise_for_status()
            except Exception:
                if attempt == MAX_FETCH_ATTEMPTS:
                    logger.warning(
                        "hikes scrape: %s fetch failed for %s after %d attempts",
                        stage,
                        url,
                        attempt,
                        exc_info=True,
                    )
                    _bump(stats, f"{stage}_fetch_errors")
                    return None
                await asyncio.sleep(RETRY_BACKOFF_SECS * attempt)
                continue
            await asyncio.sleep(POLITENESS_DELAY_SECS)
            return response.text
        return None


async def fetch_all_walks(client: httpx.AsyncClient) -> tuple[list[Walk], dict]:
    """Run the full scrape ladder: homepage -> areas -> sub-areas -> walk pages.

    Never raises: every fetch or parse failure is logged and counted in the
    returned stats dict (the old ErrorCollector spirit), and whatever was
    scraped successfully is returned. The caller should pass a client with
    follow_redirects=True (the old requests session followed redirects).
    """
    stats: dict[str, int] = {}
    semaphore = asyncio.Semaphore(CONCURRENCY)

    homepage_html = await _fetch(client, BASE_URL, semaphore, stats, "homepage")
    if homepage_html is None:
        logger.error("hikes scrape: failed to fetch the homepage, aborting")
        return [], stats

    area_links = parse_area_links(homepage_html, BASE_URL)
    stats["area_links"] = len(area_links)
    if not area_links:
        logger.error("hikes scrape: no area links found on homepage, aborting")
        return [], stats

    async def _fetch_and_parse(url: str, stage: str, parser) -> list[str]:
        html = await _fetch(client, url, semaphore, stats, stage)
        if html is None:
            return []
        return parser(html, url)

    sub_area_results = await asyncio.gather(
        *(_fetch_and_parse(url, "area", parse_sub_area_links) for url in area_links)
    )
    # Order-preserving dedup so shared sub-areas are only fetched once.
    sub_area_links = list(
        dict.fromkeys(link for links in sub_area_results for link in links)
    )
    stats["sub_area_links"] = len(sub_area_links)

    walk_link_results = await asyncio.gather(
        *(_fetch_and_parse(url, "sub_area", parse_walk_links) for url in sub_area_links)
    )
    walk_links = list(
        dict.fromkeys(link for links in walk_link_results for link in links)
    )
    stats["walk_links"] = len(walk_links)

    async def _fetch_walk(url: str) -> Walk | None:
        html = await _fetch(client, url, semaphore, stats, "walk")
        if html is None:
            return None
        walk = parse_walk(html)
        if walk is None:
            _bump(stats, "walk_parse_failures")
        return walk

    walk_results = await asyncio.gather(*(_fetch_walk(url) for url in walk_links))
    walks = [walk for walk in walk_results if walk is not None]
    stats["walks_parsed"] = len(walks)

    logger.info(
        "hikes scrape: parsed %d/%d walk pages (stats: %s)",
        len(walks),
        len(walk_links),
        stats,
    )
    return walks, stats
