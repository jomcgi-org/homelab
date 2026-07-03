"""Unit tests for campsites.client pure functions (no network, no DB).

Covers load_catalog (reading the static catalog.json, coordinate skip) and
merge_availability (OR logic, ragged arrays, empty payload). Async network
functions are not tested here; they would require a curl_cffi mock and are
validated by integration / CI runs.
"""

import datetime
import json

import campsites.client as client
from campsites.client import merge_availability

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_START = datetime.date(2026, 7, 1)

# A tiny catalog.json shape: one complete row, one with a website (booking_url
# derived), and one with a null latitude that load_catalog must skip.
_CATALOG = [
    {
        "resource_location_id": -2147483590,
        "park_map_id": 101,
        "name": "Bromley Rock",
        "region": "Thompson Okanagan",
        "latitude": 49.5,
        "longitude": -120.0,
        "iana_tz": "America/Vancouver",
        "website": "https://bcparks.ca/bromley-rock-park/",
        "coord_source": "geocode",
    },
    {
        "resource_location_id": -2147483580,
        "park_map_id": 202,
        "name": "No Website Park",
        "region": "",
        "latitude": 50.1,
        "longitude": -119.5,
        "iana_tz": "America/Vancouver",
        "website": "",
        "coord_source": "geocode",
    },
    {
        "resource_location_id": -9999,
        "park_map_id": 303,
        "name": "No Coords Park",
        "region": "North",
        "latitude": None,
        "longitude": None,
        "iana_tz": "America/Vancouver",
        "website": "https://bcparks.ca/no-coords-park/",
        "coord_source": "geocode",
    },
]


# ---------------------------------------------------------------------------
# load_catalog tests
# ---------------------------------------------------------------------------


def test_load_catalog_parses_rows(monkeypatch):
    monkeypatch.setattr(client, "_read_catalog_json", lambda: _CATALOG)
    rows = client.load_catalog()

    # The null-coordinate row is skipped; two valid rows remain.
    assert len(rows) == 2
    by_id = {r.resource_location_id: r for r in rows}

    row = by_id[-2147483590]
    assert row.park_map_id == 101
    assert row.name == "Bromley Rock"
    assert row.region == "Thompson Okanagan"
    assert row.latitude == 49.5
    assert row.longitude == -120.0
    assert row.iana_tz == "America/Vancouver"
    assert row.description == ""  # catalog.json carries no description
    assert row.booking_url == "https://bcparks.ca/bromley-rock-park/"


def test_load_catalog_booking_url_falls_back_to_root(monkeypatch):
    monkeypatch.setattr(client, "_read_catalog_json", lambda: _CATALOG)
    rows = client.load_catalog()
    row = next(r for r in rows if r.resource_location_id == -2147483580)
    assert row.booking_url == "https://camping.bcparks.ca/"


def test_load_catalog_skips_null_coords(monkeypatch):
    monkeypatch.setattr(client, "_read_catalog_json", lambda: _CATALOG)
    ids = [r.resource_location_id for r in client.load_catalog()]
    assert -9999 not in ids


def test_load_catalog_reads_packaged_json():
    """The committed catalog.json parses and every returned row has coordinates."""
    rows = client.load_catalog()
    assert rows, "catalog.json should yield at least one campground"
    assert all(r.latitude is not None and r.longitude is not None for r in rows)
    assert all(r.park_map_id for r in rows)


def test_read_catalog_json_shape():
    """The committed catalog.json is a non-empty list of dicts with the keys we read."""
    raw = client._read_catalog_json()
    assert isinstance(raw, list) and raw
    first = raw[0]
    for key in ("resource_location_id", "park_map_id", "name", "latitude", "longitude"):
        assert key in first


# ---------------------------------------------------------------------------
# merge_availability tests
# ---------------------------------------------------------------------------


def test_merge_availability_enum():
    """GoingToCamp enum: only code 0 is available; 1 (booked) and 2 (closed)
    are NOT. A day is open if any loop is 0; loops_open counts the 0-loops."""
    payload = {
        "mapLinkAvailabilities": {
            "-1": [0, 1, 2],  # available, booked, closed
            "-2": [1, 0, 2],  # booked, available, closed
        }
    }
    result = merge_availability(payload, _START, ndays=3)
    assert len(result) == 3

    # Day 0: loop -1 is available (0), loop -2 is booked (1): open, 1 loop.
    assert result[0].date == _START
    assert result[0].has_availability is True
    assert result[0].loops_open == 1

    # Day 1: loop -2 is available (0), loop -1 is booked (1): open, 1 loop.
    assert result[1].date == _START + datetime.timedelta(days=1)
    assert result[1].has_availability is True
    assert result[1].loops_open == 1

    # Day 2: both loops closed (2): NOT open, 0 loops. This is the regression
    # the old truthy test got wrong (it counted 2 as available).
    assert result[2].date == _START + datetime.timedelta(days=2)
    assert result[2].has_availability is False
    assert result[2].loops_open == 0


def test_merge_availability_nonzero_codes_are_not_open():
    """A loop that is all-booked (1) or all-closed (2) never reads as available,
    even though those values are truthy in Python."""
    payload = {
        "mapLinkAvailabilities": {
            "-1": [1, 1, 1],  # fully booked every day
            "-2": [2, 2, 2],  # closed every day
        }
    }
    result = merge_availability(payload, _START, ndays=3)
    assert all(not d.has_availability for d in result)
    assert all(d.loops_open == 0 for d in result)


def test_merge_availability_ragged():
    """Ragged arrays (shorter than ndays) and empty arrays do not crash."""
    payload = {
        "mapLinkAvailabilities": {
            "-10": [0],  # only one entry (available); index 1+ is missing
            "-11": [],  # empty; every index missing
        }
    }
    result = merge_availability(payload, _START, ndays=3)
    assert len(result) == 3

    # Day 0: array -10 is 0 (available) at index 0.
    assert result[0].has_availability is True
    assert result[0].loops_open == 1

    # Days 1 and 2: index out of range for -10, -11 always empty.
    assert result[1].has_availability is False
    assert result[1].loops_open == 0
    assert result[2].has_availability is False
    assert result[2].loops_open == 0


def test_merge_availability_empty_payload():
    """Missing mapLinkAvailabilities key produces ndays closed rows without crashing."""
    result = merge_availability({}, _START, ndays=3)
    assert len(result) == 3
    assert all(not d.has_availability for d in result)
    assert all(d.loops_open == 0 for d in result)


def test_merge_availability_dates_are_sequential():
    """Dates in the result are consecutive starting from start_date."""
    result = merge_availability({}, _START, ndays=5)
    for i, day in enumerate(result):
        assert day.date == _START + datetime.timedelta(days=i)


def test_catalog_json_is_valid_json():
    """The committed catalog file is itself valid JSON (guards a bad regen)."""
    raw = client._read_catalog_json()
    # Round-trips cleanly.
    assert json.loads(json.dumps(raw)) == raw
