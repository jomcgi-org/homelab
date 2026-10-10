"""PostgreSQL fixture output must not block expected constraint-error tests."""

import os
import signal
import sys

import pytest

from shared.testing.plugin import _logged_postgres


def test_postgres_capture_handles_output_larger_than_a_pipe(monkeypatch):
    monkeypatch.setattr("shared.testing.plugin._pg_preexec", None)
    size = 131072
    command = [
        sys.executable,
        "-c",
        f"import os; os.write(1, b'o' * {size}); os.write(2, b'e' * {size})",
    ]
    with _logged_postgres(command, os.environ.copy()) as (proc, output):
        assert proc.wait(timeout=10) == 0
        output.seek(0)
        assert output.read() == b"o" * size + b"e" * size
    assert output.closed


@pytest.mark.parametrize("fail_setup", [False, True])
def test_postgres_capture_stops_child_and_closes_logs(monkeypatch, fail_setup):
    monkeypatch.setattr("shared.testing.plugin._pg_preexec", None)
    command = [sys.executable, "-c", "import signal; signal.pause()"]
    try:
        with _logged_postgres(command, os.environ.copy()) as (proc, output):
            if fail_setup:
                raise RuntimeError("simulated migration setup failure")
    except RuntimeError as exc:
        assert fail_setup and str(exc) == "simulated migration setup failure"
    assert proc.poll() == -signal.SIGTERM
    assert output.closed
