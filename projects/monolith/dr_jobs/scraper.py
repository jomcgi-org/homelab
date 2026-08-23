"""Scrape NHS Scotland anaesthetics-consultant vacancies from apply.jobs.scot.nhs.uk.

The JobTrain site is a two-tier scrape:

- ``GET /Home/_JobCard?what=<kw>&Salary=<bands>&Skip=<n>`` is the AJAX list
  endpoint the vacancies page itself calls. It returns a partial HTML fragment
  of job cards (``data-jobId`` attributes) plus a ``totalMatchRecords`` hidden
  input for pagination. No auth, no antiforgery token, no reCAPTCHA on GET.
- ``GET /Job/JobDetail?JobId=<id>`` carries a schema.org ``JobPosting`` JSON-LD
  block (published for Google Jobs indexing). We parse the structured fields
  from there rather than scraping presentational HTML, so a cosmetic redesign
  of the site does not break us.

The pure parse helpers (parse_job_ids, parse_detail) take HTML strings so the
unit tests can exercise them against fixtures with no network. fetch_vacancies
orchestrates the network phase and never raises: it logs and counts failures in
the stats dict and returns whatever it managed to gather, mirroring the
hikes/ships scrape handlers (a transient outage must not wipe the corpus).
"""

from __future__ import annotations

import html as ihtml
import json
import logging
import os
import re
from datetime import date

import httpx

logger = logging.getLogger("monolith.dr_jobs")

BASE_URL = "https://apply.jobs.scot.nhs.uk"

# Filter scope. Defaults match the partner's saved search: keyword "anaesth"
# across the two consultant salary bands (52 = Consultant, 63 = Locum
# Consultant) from the site's Salary multiselect. Overridable via env without a
# redeploy of code, only a values bump.
DEFAULT_WHAT = os.environ.get("DR_JOBS_WHAT", "anaesth")
DEFAULT_SALARY = os.environ.get("DR_JOBS_SALARY", "52,63")

# The list endpoint pages 12 cards at a time (pageSize in vacancies.js). MAX_SKIP
# is a safety bound so a malformed totalMatchRecords cannot loop forever; the
# anaesthetics-consultant search returns well under a page in practice.
PAGE_SIZE = 12
MAX_SKIP = 600

# Per-request timeouts; the client-level timeout in the handler is the ceiling.
LIST_TIMEOUT_SECS = 15.0
DETAIL_TIMEOUT_SECS = 20.0

# The site treats these as AJAX/browser requests; mirror what vacancies.js sends.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; dr-jobs-aggregator/1.0; "
        "+https://jomcgi.dev/app/dr-jobs)"
    ),
    "X-Requested-With": "XMLHttpRequest",
}

_JOB_ID_RE = re.compile(r'data-jobId="(\d+)"')
_TOTAL_RE = re.compile(r'id="totalMatchRecords"\s+value="(\d+)"')
_JSONLD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)
# Leading board reference code in a title, e.g. "PS246039 - Consultant ...".
_REF_RE = re.compile(r"^\s*([A-Z]{2,}\d[\w-]*)\b")


def parse_job_ids(list_html: str) -> tuple[list[str], int | None]:
    """Parse the _JobCard fragment: ordered unique JobIds and the match total.

    Returns (ids, total). total is None if the hidden input is absent (treated
    by the caller as "stop paginating").
    """
    ids: list[str] = []
    for jid in _JOB_ID_RE.findall(list_html):
        if jid not in ids:
            ids.append(jid)
    m = _TOTAL_RE.search(list_html)
    total = int(m.group(1)) if m else None
    return ids, total


def _parse_iso_date(value: str | None) -> date | None:
    """Parse an ISO date or datetime prefix (validThrough is '...T00:00')."""
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def parse_detail(detail_html: str, job_id: str, url: str) -> dict | None:
    """Extract a vacancy dict from a JobDetail page's JSON-LD JobPosting block.

    Returns None if no parseable JobPosting is present (the caller counts it as
    a detail error and skips). Salary and title are HTML-unescaped (the JSON-LD
    carries entities like &#xA3; for the pound sign).
    """
    m = _JSONLD_RE.search(detail_html)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("@type") != "JobPosting":
        return None

    title = ihtml.unescape((data.get("title") or "").strip())
    if not title:
        return None
    salary_text = ihtml.unescape((data.get("baseSalary") or "").strip())
    # "Consultant (£111,430 - £148,064)" -> "Consultant"; bare text -> itself.
    salary_band = salary_text.split(" (", 1)[0].strip()

    ref_match = _REF_RE.match(title)
    reference = ref_match.group(1) if ref_match else ""

    loc = data.get("jobLocation") or {}
    address = loc.get("address") if isinstance(loc, dict) else {}
    address = address if isinstance(address, dict) else {}

    return {
        "job_id": job_id,
        "reference": reference,
        "title": title,
        "employment_type": (data.get("employmentType") or "").strip(),
        "salary_band": salary_band,
        "salary_text": salary_text,
        "town": (address.get("addressLocality") or "").strip(),
        "postcode": (address.get("postalCode") or "").strip(),
        "region": (address.get("addressRegion") or "").strip(),
        "posted_date": _parse_iso_date(data.get("datePosted")),
        "closing_date": _parse_iso_date(data.get("validThrough")),
        "url": url,
    }


async def _list_job_ids(
    client: httpx.AsyncClient, what: str, salary: str, stats: dict
) -> list[str]:
    """Page through _JobCard collecting every matching JobId (dedup, in order)."""
    job_ids: list[str] = []
    skip = 0
    while skip <= MAX_SKIP:
        params = {"what": what, "Salary": salary, "Skip": skip}
        try:
            resp = await client.get(
                f"{BASE_URL}/Home/_JobCard",
                params=params,
                headers=_HEADERS,
                timeout=LIST_TIMEOUT_SECS,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("dr_jobs list fetch failed at skip=%d: %s", skip, exc)
            stats["list_err"] += 1
            break
        ids, total = parse_job_ids(resp.text)
        before = len(job_ids)
        job_ids.extend(jid for jid in ids if jid not in job_ids)
        skip += PAGE_SIZE
        # Stop when we've covered the reported total, the page was empty, or a
        # page added nothing new (defensive against a stuck pager).
        if not ids or len(job_ids) == before or total is None or skip >= total:
            break
    return job_ids


async def fetch_vacancies(
    client: httpx.AsyncClient,
    what: str = DEFAULT_WHAT,
    salary: str = DEFAULT_SALARY,
) -> tuple[list[dict], dict]:
    """Fetch and parse every matching vacancy. Never raises.

    Returns (vacancies, stats). stats counts list/detail errors so the handler
    can log a health summary and decide whether the run produced anything.
    """
    stats = {"listed": 0, "detail_ok": 0, "detail_err": 0, "list_err": 0}
    job_ids = await _list_job_ids(client, what, salary, stats)
    stats["listed"] = len(job_ids)

    vacancies: list[dict] = []
    for job_id in job_ids:
        url = f"{BASE_URL}/Job/JobDetail?JobId={job_id}"
        try:
            resp = await client.get(url, headers=_HEADERS, timeout=DETAIL_TIMEOUT_SECS)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("dr_jobs detail fetch failed for %s: %s", job_id, exc)
            stats["detail_err"] += 1
            continue
        vac = parse_detail(resp.text, job_id, url)
        if vac is None:
            logger.warning("dr_jobs detail parse yielded nothing for %s", job_id)
            stats["detail_err"] += 1
            continue
        vacancies.append(vac)
        stats["detail_ok"] += 1
    return vacancies, stats
