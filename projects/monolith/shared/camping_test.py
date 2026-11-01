"""Offline sourced points and topology semantics for materializer membership."""

import json

import pytest

from shapely.geometry import MultiPolygon, Polygon
from shapely.prepared import prep

from shared import camping


def test_sourced_points():
    # WGS84 points: Balmaha shore, inland Trossachs, Meall a' Bhuachaille
    # start, Edinburgh. These exercise the packaged publisher geometry.
    assert camping.membership(56.084, -4.548) == {
        "zones": ["East Loch Lomond"],
        "parks": ["Loch Lomond and The Trossachs"],
    }
    assert camping.membership(56.3, -4.4) == {
        "zones": [],
        "parks": ["Loch Lomond and The Trossachs"],
    }
    assert camping.membership(57.17, -3.67) == {"zones": [], "parks": ["Cairngorms"]}
    assert camping.membership(55.95, -3.2) == {"zones": [], "parks": []}


def test_holes_multipolygons_and_boundary_points(monkeypatch):
    polygon = Polygon(
        [(0, 0), (4, 0), (4, 4), (0, 4)], [[(1, 1), (3, 1), (3, 3), (1, 3)]]
    )
    island = Polygon([(5, 5), (6, 5), (6, 6), (5, 6)])
    monkeypatch.setattr(
        camping,
        "_boundaries",
        lambda: [("zone", "test", prep(MultiPolygon([polygon, island])))],
    )
    assert camping.membership(0.5, 0.5)["zones"] == ["test"]
    assert camping.membership(2, 2)["zones"] == []
    assert camping.membership(2, 0)["zones"] == ["test"]
    assert camping.membership(2, 1)["zones"] == ["test"]  # hole edge
    assert camping.membership(5.5, 5.5)["zones"] == ["test"]
    assert camping.membership(4.5, 4.5)["zones"] == []


def test_missing_coordinates_are_unknown_and_json_is_small():
    for lat, lon in [(None, 0), (float("nan"), 0), (91, 0), (0, float("inf"))]:
        assert camping.membership(lat, lon) is None
    encoded = json.dumps(camping.membership(56.084, -4.548))
    assert len(encoded) < 150 and "coordinates" not in encoded


def test_resource_failure_cannot_claim_outside(monkeypatch):
    def missing():
        raise FileNotFoundError("producer boundary resource missing")

    monkeypatch.setattr(camping, "_boundaries", missing)
    with pytest.raises(FileNotFoundError):
        camping.membership(56.084, -4.548)
