import asyncio
from datetime import datetime, timezone

import httpx

from worldcup import client as wc_client
from worldcup.client import _get_json, build_team_index, parse_fixtures, parse_standings


def _async_client(handler):
    # MockTransport never hits the network, but set a timeout to satisfy the
    # httpx-client-no-timeout rule and mirror production usage.
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://test", timeout=5.0
    )


def _fetch(handler, monkeypatch):
    """Run _get_json against a MockTransport handler with backoff zeroed."""
    monkeypatch.setattr(wc_client, "_FETCH_BACKOFF_SECS", 0)
    stats = {"errors": []}

    async def go():
        async with _async_client(handler) as c:
            return await _get_json(c, "/get/teams", stats)

    return asyncio.run(go()), stats


def test_get_json_retries_5xx_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"ok": True})

    result, stats = _fetch(handler, monkeypatch)
    assert result == {"ok": True}
    assert calls["n"] == 2  # retried once, then succeeded
    assert stats["errors"] == []


def test_get_json_does_not_retry_4xx(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, text="nope")

    result, stats = _fetch(handler, monkeypatch)
    assert result == {}  # degrades gracefully
    assert calls["n"] == 1  # a client error is not retried
    assert len(stats["errors"]) == 1


def test_get_json_exhausts_retries_then_degrades(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, text="down")

    result, stats = _fetch(handler, monkeypatch)
    assert result == {}
    assert calls["n"] == wc_client._FETCH_ATTEMPTS  # all attempts used
    assert len(stats["errors"]) == 1


TEAMS = {
    "teams": [
        {
            "id": "12",
            "name_en": "Scotland",
            "fifa_code": "SCO",
            "flag": "f/sco",
            "groups": "C",
        },
        {
            "id": "9",
            "name_en": "Brazil",
            "fifa_code": "BRA",
            "flag": "f/bra",
            "groups": "C",
        },
        # Missing fifa_code: must be skipped (unjoinable).
        {"id": "99", "name_en": "Nowhere", "flag": "f/non"},
    ]
}


def test_build_team_index():
    idx = build_team_index(TEAMS)
    assert idx["12"]["fifa_code"] == "SCO"
    assert idx["12"]["name"] == "Scotland"
    assert idx["12"]["flag"] == "f/sco"
    assert idx["9"]["fifa_code"] == "BRA"
    # Entry without a fifa_code is dropped.
    assert "99" not in idx


def test_parse_standings_joins_and_coerces():
    idx = build_team_index(TEAMS)
    rows = parse_standings(
        {
            "groups": [
                {
                    "name": "C",
                    "teams": [
                        {
                            "team_id": "12",
                            "mp": "2",
                            "w": "1",
                            "d": "0",
                            "l": "1",
                            "pts": "3",
                            "gf": "4",
                            "ga": "1",
                            "gd": "3",
                        }
                    ],
                }
            ]
        },
        idx,
    )
    assert len(rows) == 1
    r = rows[0]
    # Identity joined in from the team index.
    assert r["team_id"] == "12"
    assert r["name"] == "Scotland"
    assert r["fifa_code"] == "SCO"
    assert r["flag_url"] == "f/sco"
    assert r["group_name"] == "C"
    # String stats coerced to real ints.
    assert r["mp"] == 2 and isinstance(r["mp"], int)
    assert r["w"] == 1 and r["d"] == 0 and r["l"] == 1
    assert r["pts"] == 3 and isinstance(r["pts"], int)
    assert r["gf"] == 4 and r["ga"] == 1 and r["gd"] == 3


def test_parse_standings_skips_team_not_in_index():
    idx = build_team_index(TEAMS)
    rows = parse_standings(
        {"groups": [{"name": "C", "teams": [{"team_id": "404", "pts": "9"}]}]},
        idx,
    )
    assert rows == []


def test_parse_fixtures_group_only_bool_and_kickoff():
    idx = build_team_index(TEAMS)
    fx = parse_fixtures(
        {
            "games": [
                {
                    "id": "5",
                    "type": "group",
                    "group": "C",
                    "matchday": "3",
                    "home_team_id": "12",
                    "home_team_name_en": "Scotland",
                    "away_team_id": "9",
                    "away_team_name_en": "Brazil",
                    "home_score": "0",
                    "away_score": "0",
                    "finished": "FALSE",
                    "local_date": "06/24/2026 18:00",
                },
                {
                    "id": "200",
                    "type": "knockout",
                    "group": "",
                    "matchday": "4",
                    "home_team_id": "12",
                    "away_team_id": "9",
                    "finished": "FALSE",
                    "local_date": "bad",
                },
            ]
        },
        idx,
    )
    # Knockout game filtered out.
    assert len(fx) == 1
    f = fx[0]
    assert f["match_id"] == "5"
    assert f["group_name"] == "C"
    assert f["matchday"] == 3 and isinstance(f["matchday"], int)
    # finished string -> bool; unfinished -> scores None.
    assert f["finished"] is False
    assert f["home_score"] is None and f["away_score"] is None
    # FIFA codes resolved via the index.
    assert f["home_code"] == "SCO" and f["away_code"] == "BRA"
    assert f["home_id"] == "12" and f["away_id"] == "9"
    assert f["home_name"] == "Scotland" and f["away_name"] == "Brazil"
    # kickoff is an aware datetime normalised to UTC.
    assert isinstance(f["kickoff"], datetime)
    assert f["kickoff"].utcoffset().total_seconds() == 0


def test_parse_fixtures_finished_scores_and_bad_kickoff():
    idx = build_team_index(TEAMS)
    fx = parse_fixtures(
        {
            "games": [
                {
                    "id": "5",
                    "type": "group",
                    "group": "C",
                    "matchday": "1",
                    "home_team_id": "9",
                    "away_team_id": "12",
                    "home_score": "1",
                    "away_score": "2",
                    "finished": "TRUE",
                    "local_date": "garbage",
                }
            ]
        },
        idx,
    )
    f = fx[0]
    # Finished -> real int scores.
    assert f["finished"] is True
    assert f["home_score"] == 1 and isinstance(f["home_score"], int)
    assert f["away_score"] == 2
    # Names fall back to the index when the game omits them.
    assert f["home_name"] == "Brazil" and f["away_name"] == "Scotland"
    # Unparseable local_date -> kickoff None, no raise.
    assert f["kickoff"] is None


def test_parse_fixtures_eastern_to_utc_offset():
    idx = build_team_index(TEAMS)
    fx = parse_fixtures(
        {
            "games": [
                {
                    "id": "7",
                    "type": "group",
                    "group": "C",
                    "matchday": "2",
                    "home_team_id": "12",
                    "away_team_id": "9",
                    "finished": "FALSE",
                    "local_date": "06/24/2026 18:00",
                }
            ]
        },
        idx,
    )
    # 18:00 US Eastern (EDT, UTC-4) in June -> 22:00 UTC.
    assert fx[0]["kickoff"] == datetime(2026, 6, 24, 22, 0, tzinfo=timezone.utc)


def test_parse_fixtures_drops_unresolved_team_codes():
    idx = build_team_index(TEAMS)
    fx = parse_fixtures(
        {
            "games": [
                # Away team id is absent from the index -> away_code None -> dropped.
                {
                    "id": "8",
                    "type": "group",
                    "group": "C",
                    "matchday": "1",
                    "home_team_id": "12",
                    "away_team_id": "404",
                    "finished": "FALSE",
                    "local_date": "06/24/2026 18:00",
                },
                # Fully resolvable game is still kept.
                {
                    "id": "9",
                    "type": "group",
                    "group": "C",
                    "matchday": "1",
                    "home_team_id": "12",
                    "away_team_id": "9",
                    "finished": "FALSE",
                    "local_date": "06/24/2026 18:00",
                },
            ]
        },
        idx,
    )
    # Unresolved game dropped, resolvable game survives.
    assert len(fx) == 1
    assert fx[0]["match_id"] == "9"
    assert fx[0]["home_code"] == "SCO" and fx[0]["away_code"] == "BRA"
