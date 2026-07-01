"""Unit tests for campsites.client pure functions (no network, no DB).

Covers parse_catalog (catalog joining, name resolution, GPS extraction,
drop conditions) and merge_availability (OR logic, ragged arrays, empty
payload). Async network functions are not tested here; they would require
httpx.MockTransport and are validated by integration / CI runs.
"""

import datetime

from campsites.client import DayAvail, merge_availability, parse_catalog

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Two resource location entries; the second has no matching mapLink and must
# be dropped by parse_catalog.
_RESOURCE_JSON = [
    {
        "resourceLocationId": -2147483590,
        "gpsCoordinates": {"latitude": 49.5, "longitude": -120.0},
        "ianaTimeZone": "America/Vancouver",
        "region": "Thompson Okanagan",
        "localizedValues": [
            {
                "cultureName": "en-CA",
                "shortName": "Bromley Rock",
                "fullName": "Bromley Rock Provincial Park",
                "description": "A beautiful riverside park.",
            }
        ],
    },
    # No mapLink entry for this ID: must be silently dropped.
    {
        "resourceLocationId": -9999,
        "gpsCoordinates": {"latitude": 50.0, "longitude": -121.0},
        "ianaTimeZone": "America/Vancouver",
        "region": "North",
        "localizedValues": [
            {"cultureName": "en-CA", "shortName": "Orphan Park", "fullName": "Orphan"}
        ],
    },
]

_MAPS_JSON = [
    {
        "mapLinks": [
            {
                "resourceLocationId": -2147483590,
                "childMapId": 101,
                "localizations": [
                    {"cultureName": "en-CA", "title": "Bromley Rock Map"}
                ],
            },
            # No resourceLocationId: must be ignored.
            {
                "childMapId": 200,
                "localizations": [{"cultureName": "en-CA", "title": "Generic Map"}],
            },
        ]
    }
]

_START = datetime.date(2026, 7, 1)

# ---------------------------------------------------------------------------
# parse_catalog tests
# ---------------------------------------------------------------------------


def test_parse_catalog_returns_matched_row():
    rows = parse_catalog(_RESOURCE_JSON, _MAPS_JSON)
    assert len(rows) == 1
    row = rows[0]
    assert row.resource_location_id == -2147483590
    assert row.park_map_id == 101
    assert row.name == "Bromley Rock"  # shortName preferred
    assert row.region == "Thompson Okanagan"
    assert row.latitude == 49.5
    assert row.longitude == -120.0
    assert row.iana_tz == "America/Vancouver"
    assert row.description == "A beautiful riverside park."
    assert row.booking_url == "https://camping.bcparks.ca/"


def test_parse_catalog_drops_entry_with_no_map_link():
    rows = parse_catalog(_RESOURCE_JSON, _MAPS_JSON)
    ids = [r.resource_location_id for r in rows]
    assert -9999 not in ids


def test_parse_catalog_name_fallback_to_fullname():
    resource = [
        {
            "resourceLocationId": -2147483590,
            "gpsCoordinates": {"latitude": 49.5, "longitude": -120.0},
            "ianaTimeZone": "America/Vancouver",
            "region": "Thompson Okanagan",
            "localizedValues": [
                {
                    "cultureName": "en-CA",
                    "shortName": "",
                    "fullName": "Bromley Rock Provincial Park",
                    "description": "",
                }
            ],
        }
    ]
    rows = parse_catalog(resource, _MAPS_JSON)
    assert rows[0].name == "Bromley Rock Provincial Park"


def test_parse_catalog_name_fallback_to_map_title():
    resource = [
        {
            "resourceLocationId": -2147483590,
            "gpsCoordinates": {"latitude": 49.5, "longitude": -120.0},
            "ianaTimeZone": "America/Vancouver",
            "region": "Thompson Okanagan",
            "localizedValues": [
                {
                    "cultureName": "en-CA",
                    "shortName": "",
                    "fullName": "",
                    "description": "",
                }
            ],
        }
    ]
    rows = parse_catalog(resource, _MAPS_JSON)
    assert rows[0].name == "Bromley Rock Map"


def test_parse_catalog_drops_entry_with_no_gps():
    resource = [
        {
            "resourceLocationId": -2147483590,
            "gpsCoordinates": None,
            "ianaTimeZone": "America/Vancouver",
            "region": "Thompson Okanagan",
            "localizedValues": [{"cultureName": "en-CA", "shortName": "No GPS"}],
        }
    ]
    rows = parse_catalog(resource, _MAPS_JSON)
    assert rows == []


def test_parse_catalog_nested_gps_shape():
    """gpsCoordinates with a nested dict value (e.g. {"point": {lat, lon}})."""
    resource = [
        {
            "resourceLocationId": -2147483590,
            "gpsCoordinates": {"point": {"latitude": 50.1, "longitude": -119.5}},
            "ianaTimeZone": "America/Vancouver",
            "region": "Okanagan",
            "localizedValues": [{"cultureName": "en-CA", "shortName": "Nested Park"}],
        }
    ]
    rows = parse_catalog(resource, _MAPS_JSON)
    assert len(rows) == 1
    assert rows[0].latitude == 50.1
    assert rows[0].longitude == -119.5


def test_parse_catalog_keeps_first_map_link_per_location():
    """When a resourceLocationId appears in two mapLinks, only the first is kept."""
    maps = [
        {
            "mapLinks": [
                {
                    "resourceLocationId": -2147483590,
                    "childMapId": 101,
                    "localizations": [{"cultureName": "en-CA", "title": "First Map"}],
                },
                {
                    "resourceLocationId": -2147483590,
                    "childMapId": 999,
                    "localizations": [{"cultureName": "en-CA", "title": "Second Map"}],
                },
            ]
        }
    ]
    rows = parse_catalog(_RESOURCE_JSON[:1], maps)
    assert rows[0].park_map_id == 101


# ---------------------------------------------------------------------------
# merge_availability tests
# ---------------------------------------------------------------------------


def test_merge_availability_or():
    """OR logic: day open if any loop has 1; loops_open counts active loops."""
    payload = {
        "mapLinkAvailabilities": {
            "-1": [0, 1, 0],
            "-2": [1, 1, 0],
        }
    }
    result = merge_availability(payload, _START, ndays=3)
    assert len(result) == 3

    # Day 0: loop -2 is open (1), loop -1 is closed (0): has_availability=True, loops_open=1
    assert result[0].date == _START
    assert result[0].has_availability is True
    assert result[0].loops_open == 1

    # Day 1: both loops open: has_availability=True, loops_open=2
    assert result[1].date == _START + datetime.timedelta(days=1)
    assert result[1].has_availability is True
    assert result[1].loops_open == 2

    # Day 2: both loops closed: has_availability=False, loops_open=0
    assert result[2].date == _START + datetime.timedelta(days=2)
    assert result[2].has_availability is False
    assert result[2].loops_open == 0


def test_merge_availability_ragged():
    """Ragged arrays (shorter than ndays) and empty arrays do not crash."""
    payload = {
        "mapLinkAvailabilities": {
            "-10": [1],  # only one entry; index 1+ treated as 0
            "-11": [],  # empty; all indices treated as 0
        }
    }
    result = merge_availability(payload, _START, ndays=3)
    assert len(result) == 3

    # Day 0: array -10 has 1 at index 0.
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
