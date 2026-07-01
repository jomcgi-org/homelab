"""Unit tests for campsites.weather pure functions.

Scores are computed from the formula in weather.py and hard-coded here as the
product spec. Do NOT adjust expected values without also updating the formula
and the module-level constants.

Formula recap (from weather.py):
  base = (100 - cloud_cover) * CLEAR_WEIGHT         (CLEAR_WEIGHT = 1.0)
  precip_pen = min(PRECIP_PENALTY_CAP,               (cap = 45.0)
                   prob * PRECIP_PENALTY_PER_PCT      (0.4 per pct)
                   + sum * PRECIP_PENALTY_PER_MM)     (8.0 per mm)
  temp_pen: 0 in [15, 28]; (LO-temp)*2 below, (temp-HI)*2 above; capped at 20
  score = clamp(0, 100, round(base - precip_pen - temp_pen))
"""

import datetime

import pytest

from campsites.weather import (
    GOOD_PRECIP_MAX_MM,
    GOOD_SCORE_MIN,
    WxDay,
    is_good_day,
    parse_forecast,
    sunny_score,
)


# ---------------------------------------------------------------------------
# sunny_score
# ---------------------------------------------------------------------------


def test_sunny_score_clear_dry_mild():
    # cloud=5 -> base=95; precip=0,prob=0 -> pen=0; temp=22 in [15,28] -> pen=0
    # score = round(95 - 0 - 0) = 95
    assert (
        sunny_score(cloud_cover=5, precip_sum=0.0, precip_prob=0.0, temp_max=22.0) == 95
    )


def test_sunny_score_clear_dry_mild_is_good():
    score = sunny_score(cloud_cover=5, precip_sum=0.0, precip_prob=0.0, temp_max=22.0)
    assert is_good_day(score, precip_sum=0.0) is True


def test_sunny_score_overcast():
    # cloud=90 -> base=10; precip=0,prob=0 -> pen=0; temp=20 in [15,28] -> pen=0
    # score = round(10 - 0 - 0) = 10
    assert (
        sunny_score(cloud_cover=90, precip_sum=0.0, precip_prob=0.0, temp_max=20.0)
        == 10
    )


def test_sunny_score_overcast_is_not_good():
    score = sunny_score(cloud_cover=90, precip_sum=0.0, precip_prob=0.0, temp_max=20.0)
    assert is_good_day(score, precip_sum=0.0) is False


def test_sunny_score_clear_but_rainy():
    # cloud=10 -> base=90
    # prob=80 * 0.4 + sum=6.0 * 8.0 = 32 + 48 = 80, capped at 45
    # temp=22 in [15,28] -> temp_pen=0
    # score = round(90 - 45 - 0) = 45
    assert (
        sunny_score(cloud_cover=10, precip_sum=6.0, precip_prob=80.0, temp_max=22.0)
        == 45
    )


def test_sunny_score_clear_but_rainy_is_not_good_precip_gate():
    # Precipitation gate: precip_sum=6.0 >= GOOD_PRECIP_MAX_MM=3.0 -> False
    score = sunny_score(cloud_cover=10, precip_sum=6.0, precip_prob=80.0, temp_max=22.0)
    assert is_good_day(score, precip_sum=6.0) is False


def test_sunny_score_cold():
    # cloud=10 -> base=90
    # precip=0,prob=0 -> precip_pen=0
    # temp=2 < COMFORT_LO=15: temp_pen = min(20, (15-2)*2) = min(20, 26) = 20
    # score = round(90 - 0 - 20) = 70
    assert (
        sunny_score(cloud_cover=10, precip_sum=0.0, precip_prob=0.0, temp_max=2.0) == 70
    )


def test_sunny_score_clamped_never_below_zero():
    # Worst possible inputs: total penalty exceeds base.
    # base=(100-100)*1.0=0, precip_pen=min(45,100*0.4+100*8.0)=45, temp_pen=min(20,(15-(-50))*2)=20
    # raw = 0 - 45 - 20 = -65 -> clamped to 0
    assert (
        sunny_score(
            cloud_cover=100, precip_sum=100.0, precip_prob=100.0, temp_max=-50.0
        )
        == 0
    )


def test_sunny_score_clamped_never_above_hundred():
    # Perfect conditions: cloud=0, no precip, mild temp -> base=100, no penalties
    assert (
        sunny_score(cloud_cover=0, precip_sum=0.0, precip_prob=0.0, temp_max=22.0)
        == 100
    )


def test_sunny_score_none_inputs():
    # All None treated as 0 for base and penalties; temp None -> no temp penalty
    # base=(100-0)*1.0=100, precip_pen=0, temp_pen=0 -> score=100
    assert (
        sunny_score(cloud_cover=None, precip_sum=None, precip_prob=None, temp_max=None)
        == 100
    )


# ---------------------------------------------------------------------------
# is_good_day
# ---------------------------------------------------------------------------


def test_is_good_day_score_just_below_threshold():
    assert is_good_day(GOOD_SCORE_MIN - 1, precip_sum=0.0) is False


def test_is_good_day_score_at_threshold_dry():
    assert is_good_day(GOOD_SCORE_MIN, precip_sum=0.0) is True


def test_is_good_day_score_high_but_rainy():
    # Precipitation gate: precip >= GOOD_PRECIP_MAX_MM
    assert is_good_day(90, precip_sum=4.0) is False


def test_is_good_day_precip_exactly_at_limit():
    # 3.0 mm is NOT strictly less than GOOD_PRECIP_MAX_MM=3.0 -> False
    assert is_good_day(90, precip_sum=GOOD_PRECIP_MAX_MM) is False


def test_is_good_day_precip_just_under_limit():
    assert is_good_day(90, precip_sum=2.99) is True


# ---------------------------------------------------------------------------
# parse_forecast
# ---------------------------------------------------------------------------


def _make_daily(n: int = 3) -> dict:
    """Minimal daily payload with 3 days; day 2 has null precipitation values."""
    return {
        "time": ["2026-07-01", "2026-07-02", "2026-07-03"],
        "cloud_cover_mean": [5.0, 90.0, 30.0],
        "precipitation_sum": [0.0, None, 1.0],
        "precipitation_probability_max": [0.0, None, 30.0],
        "temperature_2m_max": [22.0, 20.0, 22.0],
        "wind_speed_10m_max": [10.0, 5.0, 15.0],
    }


def test_parse_forecast_returns_correct_row_count():
    rows = parse_forecast(_make_daily())
    assert len(rows) == 3


def test_parse_forecast_dates():
    rows = parse_forecast(_make_daily())
    assert rows[0].date == datetime.date(2026, 7, 1)
    assert rows[1].date == datetime.date(2026, 7, 2)
    assert rows[2].date == datetime.date(2026, 7, 3)


def test_parse_forecast_scores():
    # Day 1: cloud=5,precip=0,prob=0,temp=22 -> 95
    # Day 2: cloud=90,precip=None->0,prob=None->0,temp=20 -> 10
    # Day 3: cloud=30,precip=1.0,prob=30,temp=22 -> base=70,pen=min(45,12+8)=20 -> 50
    rows = parse_forecast(_make_daily())
    assert rows[0].sunny_score == 95
    assert rows[1].sunny_score == 10
    assert rows[2].sunny_score == 50


def test_parse_forecast_is_good():
    rows = parse_forecast(_make_daily())
    assert rows[0].is_good is True  # score=95, precip=0.0 < 3.0
    assert rows[1].is_good is False  # score=10 < 60
    assert rows[2].is_good is False  # score=50 < 60


def test_parse_forecast_null_precip_stored_as_none():
    rows = parse_forecast(_make_daily())
    assert rows[1].precip_sum is None
    assert rows[1].precip_prob is None


def test_parse_forecast_ndays_cap():
    # ndays=1 should return only the first row
    rows = parse_forecast(_make_daily(), ndays=1)
    assert len(rows) == 1
    assert rows[0].date == datetime.date(2026, 7, 1)


def test_parse_forecast_missing_key():
    # If a data array key is absent, all values for that field are None
    daily = {
        "time": ["2026-07-01"],
        "cloud_cover_mean": [20.0],
        # precipitation_sum intentionally absent
        "precipitation_probability_max": [0.0],
        "temperature_2m_max": [22.0],
        "wind_speed_10m_max": [5.0],
    }
    rows = parse_forecast(daily)
    assert len(rows) == 1
    assert rows[0].precip_sum is None
    # cloud=20,precip=None->0,prob=0,temp=22 -> base=80, pen=0, temp_pen=0 -> 80
    assert rows[0].sunny_score == 80
