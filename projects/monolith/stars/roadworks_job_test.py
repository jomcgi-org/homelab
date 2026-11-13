"""Real SRWR records, hostile archives, and last-good publication semantics."""

import csv
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from stars import roadworks_job as job

MODIFIED = datetime(2026, 11, 13, 1, 8, 5, tzinfo=UTC)
NOW = datetime(2026, 11, 13, 3, tzinfo=UTC)
SAMPLE = Path(__file__).parent / "testdata" / "srwr-2026-11-13.csv"
CONTRACT = (
    Path(__file__).parents[2]
    / "scotland-connector/internal/fixture/testdata/roadworks.json"
)


def rows():
    return list(csv.DictReader(io.StringIO(SAMPLE.read_text())))


def archive(records=None, bom=False):
    out = io.StringIO(newline="")
    w = csv.DictWriter(out, fieldnames=list(rows()[0]))
    w.writeheader()
    w.writerows(rows() if records is None else records)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("CurrentActivities.csv", ("\ufeff" if bom else "") + out.getvalue())
    return buf.getvalue()


def build(records=None, **kwargs):
    return json.loads(job.materialize(archive(records, **kwargs), MODIFIED, NOW))


def test_provider_sample_and_python_to_go_contract():
    actual = job.materialize(archive(), MODIFIED, NOW)
    assert json.loads(actual) == json.loads(CONTRACT.read_bytes())
    d = json.loads(actual)
    assert len(d["activities"]) == 5
    assert {a["control"] for a in d["activities"]} == {"Road Closure", "Convoy Working"}
    assert any("amnesties at 2000" in a["timing"] for a in d["activities"])
    assert not any(a["road"].startswith("A83 ") for a in d["activities"])
    assert build(bom=True) == d
    assert build(rows() + rows()) == d


def test_large_wkt_quoted_commas_and_embedded_newlines():
    r = rows()[0]
    r["GeometryFull"] = "LINESTRING (" + "123456 789012, " * 15000 + "123457 789013)"
    r["Description"] = 'Night works, "amnesty"\r\n20:00 to 05:00'
    assert (
        build([r])["activities"][0]["timing"] == 'Night works, "amnesty" 20:00 to 05:00'
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("Street", "B82 From A82 Junction"),
        ("TrafficManagement", "Portable Traffic Lights (TTLS)"),
        ("ActivityStatus", "Potential"),
        ("ActivityStatus", "Completed"),
        ("Category", "Emergency"),
    ],
)
def test_filters_produce_valid_empty_feed(field, value):
    r = rows()[0]
    r[field] = value
    assert build([r])["activities"] == []
    assert build([])["activities"] == []


def test_explicit_trunk_road_counts():
    r = rows()[0]
    r["Street"] = "M8 Trunk Road"
    assert len(build([r])["activities"]) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("Version", "01"),
        ("Latitude", "nan"),
        ("Longitude", "0"),
        ("GeometryCentroid", "POINT (0 0)"),
        ("StartDateTimeUTC", "2026-11-13"),
        ("EndDateTimeUTC", "2020-01-01T00:00:00Z"),
        ("PhaseNumber", ""),
    ],
)
def test_bad_source_never_writes(field, value):
    r = rows()[0]
    r[field] = value
    s3 = Mock()
    with pytest.raises(ValueError):
        job.publish(s3, "stars", archive([r]), MODIFIED, NOW)
    s3.put_object.assert_not_called()


def test_duplicate_conflict_headers_archive_and_resource_limits(monkeypatch):
    r = rows()[0]
    different = {**r, "Description": "different notice"}
    with pytest.raises(ValueError, match="conflicting"):
        build([r, different])
    for raw in (b"not a zip",):
        with pytest.raises(zipfile.BadZipFile):
            job.materialize(raw, MODIFIED, NOW)
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("../CurrentActivities.csv", SAMPLE.read_bytes())
    with pytest.raises(ValueError, match="requires one"):
        job.materialize(b.getvalue(), MODIFIED, NOW)
    monkeypatch.setattr(job, "MAX_ROWS", 1)
    with pytest.raises(ValueError, match="rows"):
        build()
    monkeypatch.setattr(job, "MAX_DOWNLOAD", 1)
    with pytest.raises(ValueError, match="download"):
        build()


def test_publication_and_source_clocks():
    s3 = Mock()
    job.publish(s3, "stars", archive([]), MODIFIED, NOW)
    published = json.loads(s3.put_object.call_args.kwargs["Body"])
    assert published["source_modified_at"] == "2026-11-13T01:08:05Z"
    assert published["activities"] == []
    s3.reset_mock()
    with pytest.raises(ValueError, match="freshness"):
        job.publish(
            s3, "stars", archive(), MODIFIED, datetime(2026, 11, 15, tzinfo=UTC)
        )
    s3.put_object.assert_not_called()


def test_redirects_status_and_tokens_never_escape(caplog):
    secret = "?sig=temporary-secret"
    for status in (403, 304, 302):

        def handler(request):
            return httpx.Response(
                status, headers={"Location": "https://public.example/export" + secret}
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError) as exc:
                job.download(client, lambda: NOW)
        assert secret not in str(exc.value)
        assert secret not in caplog.text

    def ok(request):
        if request.url.host == "downloads.srwr.scot":
            return httpx.Response(
                302, headers={"Location": "https://public.example/export" + secret}
            )
        return httpx.Response(
            200,
            content=archive(),
            headers={"Last-Modified": "Fri, 13 Nov 2026 01:08:05 GMT"},
        )

    with httpx.Client(transport=httpx.MockTransport(ok)) as client:
        raw, modified = job.download(client, lambda: NOW)
    assert modified == MODIFIED
    assert len(json.loads(job.materialize(raw, modified, NOW))["activities"]) == 5
    assert secret not in caplog.text


def test_archive_headers_and_expansion_limits(monkeypatch):
    for name, body in (("CurrentActivities.csv", b"Version,Street\r\n00,A82\r\n"),):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(name, body)
        with pytest.raises(ValueError, match="headers"):
            job.materialize(buf.getvalue(), MODIFIED, NOW)
    monkeypatch.setattr(job, "MAX_EXPANDED", 100)
    with pytest.raises(ValueError, match="expanded"):
        build()
    monkeypatch.setattr(job, "MAX_EXPANDED", 64 << 20)
    monkeypatch.setattr(job, "MAX_ACTIVITIES", 1)
    with pytest.raises(ValueError, match="activities"):
        build()
    monkeypatch.setattr(job, "MAX_ACTIVITIES", 5000)
    monkeypatch.setattr(job, "MAX_OUTPUT", 100)
    with pytest.raises(ValueError, match="snapshot"):
        build()
