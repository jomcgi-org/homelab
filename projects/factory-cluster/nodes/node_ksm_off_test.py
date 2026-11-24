"""Run ksm-off.sh against a fake KSM file and a fake tmpfiles.d directory."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("ksm-off.sh").resolve()
CONF = Path(__file__).with_name("factory-ksm.conf").resolve()


def run(tmp_path, value):
    ksm = tmp_path / "run"
    if value is not None:
        ksm.write_text(value + "\n")
    tmpfiles = tmp_path / "tmpfiles.d"
    done = subprocess.run(
        ["bash", str(SCRIPT)],
        env={**os.environ, "KSM_RUN": str(ksm), "TMPFILES_DIR": str(tmpfiles)},
        capture_output=True,
        text=True,
        check=False,
    )
    return done, ksm, tmpfiles


@pytest.mark.parametrize("before", ["1", "2", "0"])
def test_ksm_is_written_to_zero_and_the_boot_setting_is_installed(tmp_path, before):
    done, ksm, tmpfiles = run(tmp_path, before)
    assert done.returncode == 0, done.stderr
    assert ksm.read_text().strip() == "0"
    assert (tmpfiles / "factory-ksm.conf").read_text() == CONF.read_text()
    assert f"run was {before}, is 0" in done.stdout


def test_the_boot_setting_writes_zero_to_the_ksm_file():
    lines = [
        line.split()
        for line in CONF.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines == [["w", "/sys/kernel/mm/ksm/run", "-", "-", "-", "-", "0"]]


def test_a_second_run_changes_nothing(tmp_path):
    done, ksm, tmpfiles = run(tmp_path, "1")
    assert done.returncode == 0
    again = subprocess.run(
        ["bash", str(SCRIPT)],
        env={**os.environ, "KSM_RUN": str(ksm), "TMPFILES_DIR": str(tmpfiles)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert again.returncode == 0
    assert "run was 0, is 0" in again.stdout
    assert ksm.read_text().strip() == "0"


def test_a_kernel_without_ksm_is_not_an_error(tmp_path):
    done, ksm, tmpfiles = run(tmp_path, None)
    assert done.returncode == 0, done.stderr
    assert not ksm.exists()
    assert "nothing to turn off" in done.stdout
    assert (tmpfiles / "factory-ksm.conf").exists()
