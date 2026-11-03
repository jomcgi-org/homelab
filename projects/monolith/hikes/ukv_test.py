"""Shared producer/Go consumer fixture, without network or real storage."""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hikes import ukv

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "scotland-connector/internal/summits/testdata/summits.json"
)
NOW = datetime(2026, 10, 20, 7, tzinfo=timezone.utc)


def snapshot():
    return json.loads(FIXTURE.read_bytes())


def test_shared_contract_and_fake_publish(monkeypatch):
    wire = snapshot()
    assert ukv.build_payload(wire, NOW, fixture=True) == wire
    raw = copy.deepcopy(wire)
    raw["points"][0]["kind"] = "walk_start"
    raw["points"][0]["elevation_m_asl"] = None
    writes = []

    class Storage:
        def put_object(self, **kwargs):
            writes.append(kwargs)

    monkeypatch.setenv("UKV_PRODUCER_ENABLED", "true")
    size = ukv.materialize_sync(collect=lambda: raw, storage=Storage(), now=NOW)
    assert size == len(writes[0]["Body"])
    assert writes[0]["Key"] == "summits.json"
    produced = json.loads(writes[0]["Body"])
    assert produced["fixture"] is False
    assert produced["points"][0]["kind"] == "walk_start"
    assert produced["points"][0]["elevation_m_asl"] is None
    assert produced["points"][0]["hours"][1]["wind_kmh"] is None


@pytest.mark.parametrize("flag", [None, "false", "TRUE", "1", "typo"])
def test_disabled_zero_io(monkeypatch, flag):
    monkeypatch.delenv("UKV_PRODUCER_ENABLED", raising=False)
    if flag is not None:
        monkeypatch.setenv("UKV_PRODUCER_ENABLED", flag)
    monkeypatch.setenv("UKV_SAMPLED_INPUT", "/must/not/read")

    def fail():
        pytest.fail("disabled producer did I/O")

    class Storage:
        def put_object(self, **kwargs):
            fail()

    assert ukv.materialize_sync(collect=fail, storage=Storage()) is None
    assert ukv.materialize_sync() is None


def test_real_summits_refused():
    with pytest.raises(ValueError, match="walk_start"):
        ukv.build_payload(snapshot(), NOW)


@pytest.mark.parametrize(
    "field,value",
    [
        ("wind_kmh", -1),
        ("gust_kmh", 501),
        ("wind_kmh", float("nan")),
        ("cloud_base_m", -1),
        ("freezing_level_m_asl", 30000),
        ("wind_kmh", True),
        ("valid_at", "2026-10-20T08:30:00Z"),
        ("valid_at", "2026-10-20T08:00:00+01:00"),
        ("valid_at", "2026-10-20T05:00:00Z"),
        ("valid_at", "2026-10-26T08:00:00Z"),
    ],
)
def test_bad_hours(field, value):
    raw = snapshot()
    raw["points"][0]["hours"][0][field] = value
    with pytest.raises(ValueError):
        ukv.build_payload(raw, NOW, fixture=True)


@pytest.mark.parametrize(
    "field,value",
    [
        ("sampling", "unknown"),
        ("kind", "measured_summit"),
        ("sample_lat", 58),
        ("elevation_m_asl", 50000),
        ("cloud_base_reference", "elevation"),
    ],
)
def test_bad_point(field, value):
    raw = snapshot()
    raw["points"][0][field] = value
    with pytest.raises(ValueError):
        ukv.build_payload(raw, NOW, fixture=True)


def test_bounds_stale_missing_and_disordered():
    for run in ("2026-10-19T06:00:00Z", "2026-10-21T06:00:00Z"):
        raw = snapshot()
        raw["run_at"] = run
        with pytest.raises(ValueError):
            ukv.build_payload(raw, NOW, fixture=True)
    raw = snapshot()
    raw["points"][0]["hours"].reverse()
    with pytest.raises(ValueError):
        ukv.build_payload(raw, NOW, fixture=True)
    raw = snapshot()
    raw["points"] *= 8193
    with pytest.raises(ValueError):
        ukv.build_payload(raw, NOW, fixture=True)
    with pytest.raises(KeyError):
        ukv.build_payload({}, NOW)


def test_missing_values_remain_null():
    raw = snapshot()
    del raw["points"][0]["hours"][0]["wind_kmh"]
    result = ukv.build_payload(raw, NOW, fixture=True)
    assert result["points"][0]["hours"][0]["wind_kmh"] is None


def test_grid_sampling_units_and_nulls():
    grid = {
        "run_at": "2026-10-20T06:00:00Z",
        "units": {"wind": "m/s", "gust": "m/s", "height": "m"},
        "cloud_base_reference": "above_ground",
        "cells": [
            {
                "lat": 57.17,
                "lon": -3.67,
                "orography_m_asl": 300,
                "hours": [
                    {
                        "valid_at": "2026-10-20T08:00:00Z",
                        "wind": 10,
                        "gust": 15,
                        "cloud_base": 200,
                        "freezing_level": None,
                    }
                ],
            }
        ],
    }
    point = {
        "id": "start",
        "walk_id": "walk",
        "kind": "walk_start",
        "lat": 57.171,
        "lon": -3.67,
    }
    result = ukv.sample_grid(grid, [point], NOW)
    p = result["points"][0]
    assert p["sample_lat"] == 57.17 and p["lat"] == 57.171
    assert p["elevation_m_asl"] is None
    assert p["hours"][0] == {
        "valid_at": "2026-10-20T08:00:00Z",
        "wind_kmh": 36,
        "gust_kmh": 54,
        "cloud_base_m": 200,
        "freezing_level_m_asl": None,
    }
    grid["units"]["height"] = "ft"
    with pytest.raises(ValueError, match="units"):
        ukv.sample_grid(grid, [point], NOW)
