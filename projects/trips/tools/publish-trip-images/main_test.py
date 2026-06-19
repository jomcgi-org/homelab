"""Unit tests for publish-trip-images/main.py — OpticsData and GracefulShutdown.

Supplements publish_images_test.py by covering:
- GracefulShutdown: signal handling specific to the upload context
- OpticsData: default values and field presence
"""

import signal

import pytest

from main import (
    GracefulShutdown,
    OpticsData,
)


# ---------------------------------------------------------------------------
# OpticsData
# ---------------------------------------------------------------------------


class TestOpticsData:
    """Dataclass default values and field types."""

    def test_all_fields_default_to_none(self):
        optics = OpticsData()
        assert optics.light_value is None
        assert optics.iso is None
        assert optics.shutter_speed is None
        assert optics.aperture is None
        assert optics.focal_length_35mm is None

    def test_fields_can_be_set(self):
        optics = OpticsData(
            light_value=8.6,
            iso=400,
            shutter_speed="1/240",
            aperture=2.8,
            focal_length_35mm=16,
        )
        assert optics.light_value == pytest.approx(8.6)
        assert optics.iso == 400
        assert optics.shutter_speed == "1/240"
        assert optics.aperture == pytest.approx(2.8)
        assert optics.focal_length_35mm == 16

    def test_partial_fields(self):
        optics = OpticsData(iso=100)
        assert optics.iso == 100
        assert optics.light_value is None

    def test_equality_same_values(self):
        a = OpticsData(iso=400, aperture=2.8)
        b = OpticsData(iso=400, aperture=2.8)
        assert a == b

    def test_inequality_different_values(self):
        a = OpticsData(iso=400)
        b = OpticsData(iso=800)
        assert a != b


# ---------------------------------------------------------------------------
# GracefulShutdown (publish-trip-images context)
# ---------------------------------------------------------------------------


class TestGracefulShutdownPublish:
    """GracefulShutdown context manager for the upload process."""

    def test_initial_state_not_requested(self):
        gs = GracefulShutdown()
        assert gs.shutdown_requested is False

    def test_handler_sets_flag(self):
        with GracefulShutdown() as gs:
            gs._handler(signal.SIGINT, None)
            assert gs.shutdown_requested is True

    def test_second_signal_raises_system_exit(self):
        with GracefulShutdown() as gs:
            gs._handler(signal.SIGINT, None)
            with pytest.raises(SystemExit):
                gs._handler(signal.SIGINT, None)

    def test_sigterm_also_sets_flag(self):
        with GracefulShutdown() as gs:
            gs._handler(signal.SIGTERM, None)
            assert gs.shutdown_requested is True

    def test_context_manager_restores_signals(self):
        original_sigint = signal.getsignal(signal.SIGINT)
        with GracefulShutdown():
            pass
        assert signal.getsignal(signal.SIGINT) is original_sigint

    def test_enter_returns_self(self):
        gs = GracefulShutdown()
        with gs as ctx:
            assert ctx is gs
