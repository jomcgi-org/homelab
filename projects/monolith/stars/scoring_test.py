"""Unit tests for stars.scoring: the v2 clear-dark-hour predicates."""

from stars.scoring import (
    CLEAR_CLOUD_MAX_PCT,
    NAUTICAL_DARK_DEG,
    TWILIGHT_FLOOR_DEG,
    is_clear_dark_hour,
    is_dark_hour,
    is_twilight_hour,
)


def test_thresholds_are_the_contract_values():
    assert NAUTICAL_DARK_DEG == -12.0
    assert CLEAR_CLOUD_MAX_PCT == 10.0
    assert TWILIGHT_FLOOR_DEG == -10.0


class TestIsDarkHour:
    def test_exactly_minus_12_is_not_dark(self):
        # Strictly below -12, so the boundary itself does not count.
        assert is_dark_hour(-12.0) is False

    def test_just_below_minus_12_is_dark(self):
        assert is_dark_hour(-12.01) is True

    def test_deep_dark_is_dark(self):
        assert is_dark_hour(-30.0) is True

    def test_civil_twilight_is_not_dark(self):
        assert is_dark_hour(-6.0) is False

    def test_daytime_is_not_dark(self):
        assert is_dark_hour(0.0) is False


class TestIsTwilightHour:
    def test_exactly_minus_10_is_not_twilight(self):
        # Strictly below -10, so the boundary itself does not count.
        assert is_twilight_hour(-10.0) is False

    def test_just_below_minus_10_is_twilight(self):
        assert is_twilight_hour(-10.01) is True

    def test_shallower_than_floor_is_not_twilight(self):
        # Far-north midsummer, sun only reaches ~-8: nothing usable.
        assert is_twilight_hour(-8.0) is False

    def test_daytime_is_not_twilight(self):
        assert is_twilight_hour(0.0) is False

    def test_dark_implies_twilight(self):
        # Every true-dark hour (sun < -12) is also a twilight hour (sun < -10):
        # dark windows are a strict subset of twilight windows.
        for sun in (-12.01, -13.0, -18.0, -30.0):
            assert is_dark_hour(sun) is True
            assert is_twilight_hour(sun) is True

    def test_between_floor_and_dark_is_twilight_only(self):
        # -12 <= sun < -10: twilight-only fallback, not true dark.
        for sun in (-10.5, -11.0, -11.99):
            assert is_twilight_hour(sun) is True
            assert is_dark_hour(sun) is False


class TestIsClearDarkHour:
    def test_dark_and_clear(self):
        assert is_clear_dark_hour(-18.0, 9.99) is True

    def test_cloud_exactly_10_is_not_clear(self):
        # Strictly below 10%, so the boundary itself is not clear.
        assert is_clear_dark_hour(-18.0, 10.0) is False

    def test_cloud_just_under_10_is_clear(self):
        assert is_clear_dark_hour(-18.0, 9.99) is True

    def test_dark_but_cloudy_is_not_clear_dark(self):
        assert is_clear_dark_hour(-18.0, 50.0) is False

    def test_clear_but_not_dark_is_not_clear_dark(self):
        assert is_clear_dark_hour(-6.0, 0.0) is False

    def test_dark_boundary_not_met_is_not_clear_dark(self):
        assert is_clear_dark_hour(-12.0, 0.0) is False

    def test_truth_table(self):
        cases = [
            (-12.01, 9.99, True),  # dark + clear
            (-12.01, 10.0, False),  # dark + cloud at boundary
            (-12.0, 0.0, False),  # sun at boundary, not dark
            (-20.0, 0.0, True),  # deep dark + clear
            (-20.0, 100.0, False),  # deep dark + overcast
            (5.0, 0.0, False),  # daytime, clear sky
        ]
        for sun, cloud, expected in cases:
            assert is_clear_dark_hour(sun, cloud) is expected
