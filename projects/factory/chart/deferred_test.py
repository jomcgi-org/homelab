"""Run the transaction and fault-injection tests with the pinned Node runtime."""

import os
import subprocess
from pathlib import Path

import pytest


def test_deferred_transactions():
    result = subprocess.run(
        [
            os.environ.get("NODE_BIN", "node"),
            "--test",
            str(Path(__file__).with_name("deferred.test.mjs")),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        pytest.fail(result.stdout + result.stderr, pytrace=False)
