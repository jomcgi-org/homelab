"""Packaging regressions for the scratch-prep runtime image."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


PACKAGE = Path(__file__).resolve().parent


def test_blkid_is_explicitly_configured_and_locked() -> None:
    config = yaml.safe_load((PACKAGE / "apko.yaml").read_text())
    configured = set(config["contents"]["packages"])

    lock = json.loads((PACKAGE / "apko.lock.json").read_text())
    locked = {package["name"] for package in lock["contents"]["packages"]}

    assert "blkid" in configured
    assert "blkid" in locked
