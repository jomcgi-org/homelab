"""Unit tests for stars.seed: structural invariants of the dark-sky seed list."""

from stars.seed import SCOTLAND_DARK_SKY_LOCATIONS


def test_has_at_least_25_locations():
    assert len(SCOTLAND_DARK_SKY_LOCATIONS) >= 25


def test_ids_are_unique():
    ids = [loc["id"] for loc in SCOTLAND_DARK_SKY_LOCATIONS]
    assert len(ids) == len(set(ids))


def test_latitudes_within_scotland():
    for loc in SCOTLAND_DARK_SKY_LOCATIONS:
        assert 54 <= loc["lat"] <= 61, loc["id"]


def test_longitudes_within_scotland():
    for loc in SCOTLAND_DARK_SKY_LOCATIONS:
        assert -8 <= loc["lon"] <= 0, loc["id"]


def test_lp_zone_non_empty():
    for loc in SCOTLAND_DARK_SKY_LOCATIONS:
        assert loc["lp_zone"], loc["id"]
