"""Provider-clock, bounded collection and atomic publication contracts."""

import copy
import io
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import Mock

import httpx
import pytest
from botocore.exceptions import ClientError

from stars import aurora_job as job

NOW = datetime(2026, 10, 25, 18, 0, tzinfo=UTC)


def entry(*, expires=NOW + timedelta(hours=1)):
    return {
        "body": {"source": "unchanged"},
        "collected_at": job._iso(NOW - timedelta(minutes=10)),
        "expires_at": job._iso(expires),
        "last_modified": format_datetime(NOW - timedelta(minutes=20), usegmt=True),
    }


def test_met_cache_skips_io_without_refreshing_collection_clock():
    cached = entry()
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("unexpected I/O"))
    ) as client:
        actual = job.fetch_provider(client, job.MET_URL, cached, lambda: NOW, met=True)
    assert actual is cached
    assert actual["collected_at"] == job._iso(NOW - timedelta(minutes=10))


def test_304_revalidation_preserves_provider_body_and_uses_last_modified():
    cached = entry(expires=NOW)
    source = copy.deepcopy(cached)

    def handler(request):
        assert request.headers["If-Modified-Since"] == cached["last_modified"]
        assert request.headers["User-Agent"] == job.USER_AGENT
        return httpx.Response(
            304,
            headers={"Expires": format_datetime(NOW + timedelta(hours=2), usegmt=True)},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        actual = job.fetch_provider(client, job.MET_URL, cached, lambda: NOW, met=True)
    assert actual["body"] == source["body"]
    assert actual["collected_at"] == job._iso(NOW)
    assert actual["expires_at"] == job._iso(NOW + timedelta(hours=2))
    assert cached == source


@pytest.mark.parametrize("status", [301, 403, 429, 503])
def test_failures_do_not_trigger_retry_or_replace_cache(status):
    calls = []
    cached = entry(expires=NOW)
    before = copy.deepcopy(cached)

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://elsewhere.invalid"})

    with (
        httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        job.fetch_provider(client, job.MET_URL, cached, lambda: NOW, met=True)
    assert len(calls) == 1
    assert cached == before


def test_expiry_missing_has_bounded_fallback_and_no_conditional_without_cache():
    def handler(request):
        assert "If-Modified-Since" not in request.headers
        return httpx.Response(200, json={"source": "new"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        met = job.fetch_provider(client, job.MET_URL, {}, lambda: NOW, met=True)
        noaa = job.fetch_provider(client, job.NOAA_URL, {}, lambda: NOW, met=False)
    assert job._utc(met["expires_at"]) == NOW + timedelta(minutes=30)
    assert job._utc(noaa["expires_at"]) == NOW + timedelta(minutes=1)


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"x" * (job.MAX_BODY + 1)])
def test_malformed_or_oversized_payload_is_rejected(body):
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
        ) as client,
        pytest.raises(ValueError),
    ):
        job.fetch_provider(client, job.MET_URL, {}, lambda: NOW, met=True)


def test_304_without_cache_is_not_collection_evidence():
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(304))
        ) as client,
        pytest.raises(ValueError, match="304 without"),
    ):
        job.fetch_provider(client, job.MET_URL, {}, lambda: NOW, met=True)


def test_bounded_site_selection_is_stable_rounds_provider_coordinates_and_spans_inventory():
    rows = [
        {"id": f"s-{i:04}", "lat": 54.51 + i // 17 * 0.51, "lon": -8.99 + i % 17 * 0.5}
        for i in range(200)
    ]
    rows += [{"id": "bad", "lat": float("nan"), "lon": -4}]
    selected = job.select_sites(rows)
    assert selected == job.select_sites(list(reversed(rows)))
    assert len(selected) == job.MAX_SITES
    assert selected[0]["id"] == "s-0000"
    assert selected[-1]["id"] == "s-0199"
    assert job.select_sites([{"id": "point", "lat": 58.123456, "lon": -4.123456}]) == [
        {"id": "point", "lat": 58.1235, "lon": -4.1235}
    ]


def test_noaa_refresh_does_not_refresh_cached_met_clock_or_invent_terrain():
    met = entry()
    cache = {"weather": {"s:58.0:-4.0": met}}
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={"noaa": "updated"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        inputs, updated = job.build_inputs(
            client, [{"id": "s", "lat": 58.0, "lon": -4.0}], cache, {}, lambda: NOW
        )
    assert requests == [job.NOAA_URL]
    assert inputs["noaa_collected_at"] == job._iso(NOW)
    assert inputs["weather"][0]["collected_at"] == met["collected_at"]
    assert inputs["weather"][0]["terrain"] is None
    assert len(updated["weather"]) == 1


def test_profile_identity_mismatch_fails_before_publication():
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))
        ) as client,
        pytest.raises(ValueError, match="coordinates differ"),
    ):
        job.build_inputs(
            client,
            [{"id": "s", "lat": 58.0, "lon": -4.0}],
            {},
            {"s": {"lat": 59, "lon": -4}},
            lambda: NOW,
        )


def test_collect_spaces_requests_and_prunes_obsolete_cache():
    requests, waits = [], []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _, cache = job.build_inputs(
            client,
            [
                {"id": "a", "lat": 58.0, "lon": -4.0},
                {"id": "b", "lat": 59.0, "lon": -4.0},
            ],
            {"weather": {"obsolete": entry()}},
            {},
            lambda: NOW,
            sleep=waits.append,
        )
    assert len(requests) == 3
    assert waits == [1]
    assert "obsolete" not in cache["weather"]


def test_invalid_snapshot_keeps_cache_and_last_good_snapshot():
    s3 = Mock()

    def invalid(_):
        raise ValueError("bad NOAA/source clock")

    with pytest.raises(ValueError):
        job.publish(s3, "stars", {}, {}, render=invalid)
    s3.put_object.assert_not_called()


def test_publish_validates_before_writes_and_uses_only_existing_additive_keys():
    s3 = Mock()
    inputs = {"weather": [{"terrain": None}]}

    def render(actual):
        assert actual == inputs
        s3.put_object.assert_not_called()
        return b'{"validated":true}'

    assert job.publish(s3, "stars", inputs, {}, render=render) == 18
    writes = s3.put_object.call_args_list
    assert [w.kwargs["Key"] for w in writes] == [job.CACHE_KEY, job.SNAPSHOT_KEY]
    assert all(
        w.kwargs["Bucket"] == "stars" and w.kwargs["CacheControl"] == "no-store"
        for w in writes
    )


def test_storage_missing_object_only_is_optional():
    s3 = Mock()
    s3.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey"}}, "GetObject"
    )
    assert job.read_object(s3, "stars", job.TERRAIN_KEY) == {}
    s3.get_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied"}}, "GetObject"
    )
    with pytest.raises(ClientError):
        job.read_object(s3, "stars", job.TERRAIN_KEY)


def test_object_read_is_bounded_and_closed():
    stream = io.BytesIO(b"x" * (job.MAX_CACHE + 1))
    s3 = Mock()
    s3.get_object.return_value = {"Body": stream}
    with pytest.raises(ValueError, match="exceeds limit"):
        job.read_object(s3, "stars", job.CACHE_KEY)
    assert stream.closed


def test_live_entrypoint_refuses_credential_fallback(monkeypatch):
    monkeypatch.delenv("S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "https://storage.invalid")
    with pytest.raises(RuntimeError, match="S3_ACCESS_KEY_ID"):
        job.run()


def test_valid_short_met_expiry_is_not_extended_by_fallback():
    calls = []
    clock = [NOW]

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={"source": "met"},
            headers={
                "Expires": format_datetime(clock[0] + timedelta(minutes=5), usegmt=True)
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = job.fetch_provider(client, job.MET_URL, {}, lambda: clock[0], met=True)
        assert job._utc(first["expires_at"]) == NOW + timedelta(minutes=5)
        clock[0] += timedelta(minutes=10)
        job.fetch_provider(client, job.MET_URL, first, lambda: clock[0], met=True)
    assert len(calls) == 2


def test_noaa_is_revalidated_each_run_even_with_long_expiry():
    calls = []
    clock = [NOW]

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={"source": "noaa"},
            headers={"Expires": format_datetime(NOW + timedelta(hours=1), usegmt=True)},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = job.fetch_provider(
            client, job.NOAA_URL, {}, lambda: clock[0], met=False
        )
        clock[0] += timedelta(minutes=10)
        second = job.fetch_provider(
            client, job.NOAA_URL, first, lambda: clock[0], met=False
        )
    assert len(calls) == 2
    assert job._utc(second["collected_at"]) == clock[0]
