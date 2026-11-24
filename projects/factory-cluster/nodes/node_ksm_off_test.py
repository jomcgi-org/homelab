"""Run ksm-off.sh against a fake KSM file and a fake tmpfiles.d directory."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("ksm-off.sh").resolve()
CONF = Path(__file__).with_name("factory-ksm.conf").resolve()
QEMU_CONF = Path(__file__).with_name("factory-ksm-qemu.conf").resolve()


def env_for(tmp_path, qemu=False):
    unit = tmp_path / "qemu-kvm.service"
    if qemu:
        unit.write_text("[Unit]\n")
    return {
        **os.environ,
        "KSM_RUN": str(tmp_path / "run"),
        "TMPFILES_DIR": str(tmp_path / "tmpfiles.d"),
        "QEMU_UNIT": str(unit),
        "SYSTEMD_DIR": str(tmp_path / "systemd"),
    }


def run(tmp_path, value, qemu=False):
    ksm = tmp_path / "run"
    if value is not None:
        ksm.write_text(value + "\n")
    done = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env_for(tmp_path, qemu),
        capture_output=True,
        text=True,
        check=False,
    )
    return done, ksm, tmp_path / "tmpfiles.d"


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
        env=env_for(tmp_path),
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


def test_qemu_kvm_service_gets_a_drop_in_that_writes_zero_after_its_init(tmp_path):
    done, ksm, _ = run(tmp_path, "1", qemu=True)
    assert done.returncode == 0, done.stderr
    dropin = tmp_path / "systemd" / "qemu-kvm.service.d" / "10-factory-ksm.conf"
    assert dropin.read_text() == QEMU_CONF.read_text()
    lines = [
        line
        for line in QEMU_CONF.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines == [
        "[Service]",
        "ExecStartPost=/bin/sh -c 'echo 0 > /sys/kernel/mm/ksm/run'",
    ]


def test_a_node_without_qemu_gets_no_drop_in(tmp_path):
    done, _, _ = run(tmp_path, "1", qemu=False)
    assert done.returncode == 0, done.stderr
    assert not (tmp_path / "systemd").exists()
