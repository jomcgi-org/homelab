"""Unit tests for stars.scoring (ported from projects/stargazer/backend/scoring_test.py)."""

import pytest

from stars.scoring import (
    ScoredForecast,
    WeatherData,
    calculate_astronomy_score,
    is_dark_enough,
)


class TestWeatherDataValidation:
    def test_accepts_valid_data(self):
        w = WeatherData(
            cloud_area_fraction=10.0,
            relative_humidity=60.0,
            wind_speed=3.0,
            air_temperature=10.0,
            dew_point_temperature=5.0,
        )
        assert w.cloud_area_fraction == 10.0

    def test_fog_defaults_to_zero(self):
        w = WeatherData(
            cloud_area_fraction=50.0,
            relative_humidity=70.0,
            wind_speed=5.0,
            air_temperature=10.0,
            dew_point_temperature=5.0,
        )
        assert w.fog_area_fraction == 0.0

    def test_pressure_defaults_to_standard(self):
        w = WeatherData(
            cloud_area_fraction=50.0,
            relative_humidity=70.0,
            wind_speed=5.0,
            air_temperature=10.0,
            dew_point_temperature=5.0,
        )
        assert w.air_pressure_at_sea_level == 1013.25

    def test_rejects_negative_cloud_fraction(self):
        with pytest.raises(ValueError):
            WeatherData(
                cloud_area_fraction=-1.0,
                relative_humidity=50.0,
                wind_speed=5.0,
                air_temperature=10.0,
                dew_point_temperature=5.0,
            )

    def test_rejects_cloud_fraction_over_100(self):
        with pytest.raises(ValueError):
            WeatherData(
                cloud_area_fraction=101.0,
                relative_humidity=50.0,
                wind_speed=5.0,
                air_temperature=10.0,
                dew_point_temperature=5.0,
            )

    def test_rejects_negative_humidity(self):
        with pytest.raises(ValueError):
            WeatherData(
                cloud_area_fraction=50.0,
                relative_humidity=-5.0,
                wind_speed=5.0,
                air_temperature=10.0,
                dew_point_temperature=5.0,
            )

    def test_rejects_humidity_over_100(self):
        with pytest.raises(ValueError):
            WeatherData(
                cloud_area_fraction=50.0,
                relative_humidity=105.0,
                wind_speed=5.0,
                air_temperature=10.0,
                dew_point_temperature=5.0,
            )

    def test_rejects_negative_wind_speed(self):
        with pytest.raises(ValueError):
            WeatherData(
                cloud_area_fraction=50.0,
                relative_humidity=50.0,
                wind_speed=-1.0,
                air_temperature=10.0,
                dew_point_temperature=5.0,
            )

    def test_accepts_zero_wind_speed(self):
        w = WeatherData(
            cloud_area_fraction=50.0,
            relative_humidity=50.0,
            wind_speed=0.0,
            air_temperature=10.0,
            dew_point_temperature=5.0,
        )
        assert w.wind_speed == 0.0

    def test_accepts_boundary_cloud_values(self):
        w_clear = WeatherData(
            cloud_area_fraction=0.0,
            relative_humidity=50.0,
            wind_speed=5.0,
            air_temperature=10.0,
            dew_point_temperature=5.0,
        )
        assert w_clear.cloud_area_fraction == 0.0

        w_overcast = WeatherData(
            cloud_area_fraction=100.0,
            relative_humidity=50.0,
            wind_speed=5.0,
            air_temperature=10.0,
            dew_point_temperature=5.0,
        )
        assert w_overcast.cloud_area_fraction == 100.0

    def test_negative_temperature_is_valid(self):
        w = WeatherData(
            cloud_area_fraction=10.0,
            relative_humidity=50.0,
            wind_speed=3.0,
            air_temperature=-10.0,
            dew_point_temperature=-15.0,
        )
        assert w.air_temperature == -10.0


class TestScoredForecastModel:
    def test_accepts_valid_scored_forecast(self):
        sf = ScoredForecast(
            time="2024-01-15T22:00:00Z",
            score=85.0,
            cloud_area_fraction=10.0,
            relative_humidity=60.0,
            fog_area_fraction=0.0,
            wind_speed=3.0,
            air_temperature=8.0,
            dew_spread=5.0,
            air_pressure=1018.0,
        )
        assert sf.score == 85.0
        assert sf.symbol == ""

    def test_score_must_be_0_to_100(self):
        with pytest.raises(ValueError):
            ScoredForecast(
                time="2024-01-15T22:00:00Z",
                score=101.0,
                cloud_area_fraction=10.0,
                relative_humidity=60.0,
                fog_area_fraction=0.0,
                wind_speed=3.0,
                air_temperature=8.0,
                dew_spread=5.0,
                air_pressure=1018.0,
            )


class TestCalculateAstronomyScore:
    def _make_weather(self, **kwargs) -> WeatherData:
        defaults = {
            "cloud_area_fraction": 0.0,
            "relative_humidity": 40.0,
            "fog_area_fraction": 0.0,
            "wind_speed": 2.0,
            "air_temperature": 15.0,
            "dew_point_temperature": 5.0,
            "air_pressure_at_sea_level": 1030.0,
        }
        defaults.update(kwargs)
        return WeatherData(**defaults)

    def test_score_always_in_0_to_100_range(self):
        worst = self._make_weather(
            cloud_area_fraction=100.0,
            relative_humidity=100.0,
            fog_area_fraction=100.0,
            wind_speed=100.0,
            air_temperature=0.0,
            dew_point_temperature=0.0,
            air_pressure_at_sea_level=900.0,
        )
        assert 0.0 <= calculate_astronomy_score(worst) <= 100.0
        best = self._make_weather()
        assert 0.0 <= calculate_astronomy_score(best) <= 100.0

    def test_ideal_conditions_score_near_100(self):
        score = calculate_astronomy_score(self._make_weather())
        assert score >= 95.0

    def test_full_cloud_cover_penalises_heavily(self):
        clear_score = calculate_astronomy_score(
            self._make_weather(cloud_area_fraction=0.0)
        )
        cloudy_score = calculate_astronomy_score(
            self._make_weather(cloud_area_fraction=100.0)
        )
        assert clear_score - cloudy_score >= 40.0

    def test_high_humidity_penalises_score(self):
        score_low = calculate_astronomy_score(
            self._make_weather(relative_humidity=50.0)
        )
        score_high = calculate_astronomy_score(
            self._make_weather(relative_humidity=95.0)
        )
        assert score_low > score_high

    def test_calm_wind_better_than_strong_wind(self):
        base = {
            "cloud_area_fraction": 30.0,
            "relative_humidity": 75.0,
            "fog_area_fraction": 5.0,
            "air_temperature": 10.0,
            "dew_point_temperature": 7.0,
            "air_pressure_at_sea_level": 1013.0,
        }
        calm = calculate_astronomy_score(self._make_weather(wind_speed=2.0, **base))
        strong = calculate_astronomy_score(self._make_weather(wind_speed=20.0, **base))
        assert calm > strong

    def test_good_dew_spread_scores_higher(self):
        good = self._make_weather(air_temperature=15.0, dew_point_temperature=5.0)
        poor = self._make_weather(air_temperature=10.0, dew_point_temperature=9.5)
        assert calculate_astronomy_score(good) > calculate_astronomy_score(poor)

    def test_high_pressure_gives_bonus(self):
        low_p = self._make_weather(
            cloud_area_fraction=30.0, air_pressure_at_sea_level=1005.0
        )
        high_p = self._make_weather(
            cloud_area_fraction=30.0, air_pressure_at_sea_level=1025.0
        )
        assert calculate_astronomy_score(high_p) > calculate_astronomy_score(low_p)

    def test_pressure_bonus_capped_at_10(self):
        moderate = self._make_weather(
            cloud_area_fraction=50.0, air_pressure_at_sea_level=1020.0
        )
        extreme = self._make_weather(
            cloud_area_fraction=50.0, air_pressure_at_sea_level=1100.0
        )
        assert calculate_astronomy_score(extreme) == pytest.approx(
            calculate_astronomy_score(moderate), abs=0.1
        )

    def test_score_capped_at_100(self):
        perfect = self._make_weather(air_pressure_at_sea_level=9999.0)
        assert calculate_astronomy_score(perfect) <= 100.0

    def test_score_clamped_at_0(self):
        terrible = WeatherData(
            cloud_area_fraction=100.0,
            relative_humidity=100.0,
            fog_area_fraction=100.0,
            wind_speed=100.0,
            air_temperature=0.0,
            dew_point_temperature=10.0,
            air_pressure_at_sea_level=900.0,
        )
        assert calculate_astronomy_score(terrible) >= 0.0

    def test_weighted_average_components(self):
        base = WeatherData(
            cloud_area_fraction=0.0,
            relative_humidity=40.0,
            fog_area_fraction=0.0,
            wind_speed=2.0,
            air_temperature=15.0,
            dew_point_temperature=5.0,
            air_pressure_at_sea_level=1013.0,
        )
        all_cloud = WeatherData(
            cloud_area_fraction=100.0,
            relative_humidity=40.0,
            fog_area_fraction=0.0,
            wind_speed=2.0,
            air_temperature=15.0,
            dew_point_temperature=5.0,
            air_pressure_at_sea_level=1013.0,
        )
        diff = calculate_astronomy_score(base) - calculate_astronomy_score(all_cloud)
        assert 40.0 <= diff <= 60.0


class TestIsDarkEnough:
    def test_sun_at_minus_18_is_dark(self):
        assert is_dark_enough(-18.0) is True

    def test_sun_below_minus_18_is_dark(self):
        assert is_dark_enough(-20.0) is True

    def test_sun_just_above_threshold_is_not_dark(self):
        assert is_dark_enough(-17.9) is False

    def test_nautical_twilight_not_dark(self):
        assert is_dark_enough(-12.0) is False

    def test_daytime_not_dark(self):
        assert is_dark_enough(0.0) is False

    def test_custom_threshold_civil(self):
        assert is_dark_enough(-6.0, astronomical_darkness_threshold=-6.0) is True
        assert is_dark_enough(-5.9, astronomical_darkness_threshold=-6.0) is False
