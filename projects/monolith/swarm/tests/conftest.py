"""Swarm integration test fixtures."""

from __future__ import annotations

import pytest

# Import the shared testing plugin to make its fixtures available for integration tests.
pytest_plugins = ["shared.testing.plugin"]
