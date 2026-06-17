"""Unit tests for dr_jobs/scraper.py.

The pure parse helpers (parse_job_ids, parse_detail) run against inline HTML
fixtures shaped like the real apply.jobs.scot.nhs.uk responses. fetch_vacancies
runs against an httpx.MockTransport so the two-tier list -> detail flow is
exercised end to end with no network.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from dr_jobs import scraper

# A _JobCard fragment with two job cards and the match-total hidden input.
LIST_HTML = """
<input type="hidden" id="totalCurrentRecords" value="2">
<input type="hidden" id="totalMatchRecords" value="2">
<div class="job-card" data-jobId="256437"></div>
<a href="/Job/JobDetail?JobId=256437">PS246039 - Consultant Anaesthetist</a>
<div class="job-card" data-jobId="259286"></div>
<a href="/Job/JobDetail?JobId=259286">CI248883 - Fixed Term Consultant Anaesthetist</a>
"""

# A JobDetail page carrying a schema.org JobPosting JSON-LD block. &#xA3; is the
# literal HTML entity the site emits for the pound sign.
DETAIL_HTML_TEMPLATE = """
<html><head>
<script type="application/ld+json">
{{"@context":"https://schema.org","@type":"JobPosting",
"title":"{title}","datePosted":"{posted}","validThrough":"{closing}T00:00",
"employmentType":"{etype}",
"hiringOrganization":{{"@type":"Organization","name":"NHS Scotland"}},
"jobLocation":{{"@type":"Place","address":{{"@type":"PostalAddress",
"addressLocality":"{town}","addressRegion":"","postalCode":"{postcode}",
"addressCountry":"GB"}}}},
"baseSalary":"{salary}"}}
</script>
</head><body></body></html>
"""


def _detail(**kw) -> str:
    base = {
        "title": "PS246039 - Consultant Anaesthetist ",
        "posted": "2026-06-10",
        "closing": "2026-07-12",
        "etype": "Permanent",
        "town": "Elgin",
        "postcode": "IV30 1SN",
        "salary": "Consultant (&#xA3;111,430 - &#xA3;148,064)",
    }
    base.update(kw)
    return DETAIL_HTML_TEMPLATE.format(**base)


class TestParseJobIds:
    def test_extracts_ordered_unique_ids_and_total(self):
        ids, total = scraper.parse_job_ids(LIST_HTML)
        assert ids == ["256437", "259286"]
        assert total == 2

    def test_dedupes_repeated_ids(self):
        html = '<div data-jobId="1"></div><div data-jobId="1"></div><div data-jobId="2"></div>'
        ids, total = scraper.parse_job_ids(html)
        assert ids == ["1", "2"]
        assert total is None  # no totalMatchRecords input


class TestParseDetail:
    def test_full_parse(self):
        vac = scraper.parse_detail(_detail(), "256437", "https://x/JobId=256437")
        assert vac == {
            "job_id": "256437",
            "reference": "PS246039",
            "title": "PS246039 - Consultant Anaesthetist",
            "employment_type": "Permanent",
            "salary_band": "Consultant",
            "salary_text": "Consultant (£111,430 - £148,064)",
            "town": "Elgin",
            "postcode": "IV30 1SN",
            "region": "",
            "posted_date": date(2026, 6, 10),
            "closing_date": date(2026, 7, 12),
            "url": "https://x/JobId=256437",
        }

    def test_locum_band_split(self):
        vac = scraper.parse_detail(
            _detail(salary="Locum Consultant (&#xA3;111,430 - &#xA3;148,064)"),
            "1",
            "u",
        )
        assert vac["salary_band"] == "Locum Consultant"

    def test_title_without_reference(self):
        vac = scraper.parse_detail(
            _detail(title="Consultant in Paediatric Anaesthesia"), "1", "u"
        )
        assert vac["reference"] == ""
        assert vac["title"] == "Consultant in Paediatric Anaesthesia"

    def test_no_jsonld_returns_none(self):
        assert scraper.parse_detail("<html>no ld json</html>", "1", "u") is None

    def test_non_jobposting_returns_none(self):
        html = '<script type="application/ld+json">{"@type":"WebSite"}</script>'
        assert scraper.parse_detail(html, "1", "u") is None


@pytest.mark.asyncio
async def test_fetch_vacancies_end_to_end():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Home/_JobCard":
            return httpx.Response(200, text=LIST_HTML)
        if request.url.path == "/Job/JobDetail":
            job_id = request.url.params.get("JobId")
            title = (
                "PS246039 - Consultant Anaesthetist "
                if job_id == "256437"
                else "CI248883 - Fixed Term Consultant Anaesthetist "
            )
            return httpx.Response(200, text=_detail(title=title, etype="Fixed-term"))
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, timeout=httpx.Timeout(30.0)
    ) as client:
        vacancies, stats = await scraper.fetch_vacancies(client)

    assert stats["listed"] == 2
    assert stats["detail_ok"] == 2
    assert stats["detail_err"] == 0
    assert [v["job_id"] for v in vacancies] == ["256437", "259286"]
    assert vacancies[0]["reference"] == "PS246039"


@pytest.mark.asyncio
async def test_fetch_vacancies_counts_detail_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Home/_JobCard":
            return httpx.Response(200, text=LIST_HTML)
        # Both detail pages 500: the run lists 2 but parses 0.
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, timeout=httpx.Timeout(30.0)
    ) as client:
        vacancies, stats = await scraper.fetch_vacancies(client)

    assert vacancies == []
    assert stats["listed"] == 2
    assert stats["detail_err"] == 2
