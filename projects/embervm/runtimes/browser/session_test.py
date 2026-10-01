import os
import signal
import subprocess
import sys
import time
from dataclasses import replace

import pytest

from session import LaunchFailed, SessionOwner, SessionRefused, Supervisor, launch

OWNER = SessionOwner("task", "run", "principal")
COMMAND = (sys.executable, "-c", "import time; time.sleep(60)")
COMMANDS = {arm: (COMMAND,) for arm in ("mcp", "cli")}


@pytest.fixture
def supervisor(tmp_path):
    worker = Supervisor(COMMANDS, max_sessions=2, kill_grace_s=0.1, parent_dir=tmp_path)
    yield worker
    assert all(r.status == "ceased" for r in worker.shutdown())


def test_isolated_concurrent_owners(supervisor):
    first = supervisor.create(OWNER, "mcp")
    other = replace(OWNER, task_id="other", run_id="other", principal="other")
    second = supervisor.create(other, "cli")
    assert first.session_id != second.session_id
    assert first.processes[0].pid != second.processes[0].pid
    assert first.profile_dir != second.profile_dir
    assert first.evidence_dir != second.evidence_dir
    for session in (first, second):
        assert session.profile_dir != session.evidence_dir
        assert session.root.stat().st_mode & 0o777 == 0o700
        assert session.profile_dir.stat().st_mode & 0o777 == 0o700
        assert session.evidence_dir.stat().st_mode & 0o777 == 0o700
        assert os.getpgid(session.processes[0].pid) == session.processes[0].pid
    assert supervisor.attach(first.session_id, OWNER) is first
    with pytest.raises(SessionRefused, match="admission"):
        supervisor.create(OWNER, "mcp")


@pytest.mark.parametrize("field", ("task_id", "run_id", "principal"))
def test_cross_owner_attach_and_cancel_refused(supervisor, field):
    session = supervisor.create(OWNER, "mcp")
    other = replace(OWNER, **{field: "different"})
    with pytest.raises(SessionRefused, match="mismatch"):
        supervisor.attach(session.session_id, other)
    with pytest.raises(SessionRefused, match="mismatch"):
        supervisor.terminate(session.session_id, other, "cancel")
    assert session.processes[0].poll() is None


@pytest.mark.parametrize("reason", ("complete", "cancel"))
def test_termination_reaps_and_removes(supervisor, reason):
    session = supervisor.create(OWNER, "cli")
    report = supervisor.terminate(session.session_id, OWNER, reason)
    assert report.reason == reason and report.status == "ceased"
    assert report.dirs_gone and not session.root.exists()
    assert report.finished_monotonic >= report.started_monotonic
    assert report.processes[0].signal == signal.SIGTERM
    assert report.processes[0].reaped and report.processes[0].group_gone
    assert session.processes[0].returncode == -signal.SIGTERM
    assert supervisor.terminate(session.session_id, OWNER, reason) == report
    with pytest.raises(SessionRefused, match="terminated"):
        supervisor.attach(session.session_id, OWNER)


def test_ignores_sigterm_and_all_pids_reaped(tmp_path):
    script = "import signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); pathlib.Path('ready').touch(); time.sleep(60)"
    worker = Supervisor(
        {arm: ((sys.executable, "-c", script), COMMAND) for arm in COMMANDS},
        kill_grace_s=0.05,
        parent_dir=tmp_path,
    )
    try:
        session = worker.create(OWNER, "mcp")
        deadline = time.monotonic() + 5
        while not (session.root / "ready").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (session.root / "ready").exists()
        report = worker.terminate(session.session_id, OWNER, "cancel")
        assert report.status == "ceased" and len(report.processes) == 2
        assert report.processes[0].signal == signal.SIGKILL
        assert all(p.reaped and p.group_gone for p in report.processes)
    finally:
        worker.shutdown()


@pytest.mark.parametrize(
    "expiry, expected", ((5, "idle_timeout"), (10, "max_lifetime"))
)
def test_expiry_at_boundary(tmp_path, expiry, expected):
    now = [0.0]
    worker = Supervisor(
        COMMANDS,
        max_lifetime_s=10,
        idle_timeout_s=5,
        clock=lambda: now[0],
        parent_dir=tmp_path,
    )
    try:
        session = worker.create(OWNER, "mcp")
        now[0] = 4
        assert worker.attach(session.session_id, OWNER) is session
        now[0] = expiry
        if expiry == 5:
            # Attachment resets idle, so expire five seconds after the touch.
            now[0] = 9
        with pytest.raises(SessionRefused, match="expired"):
            worker.attach(session.session_id, OWNER)
        assert session.report.reason == expected and session.report.status == "ceased"
        assert session.report.finished_monotonic == now[0]
    finally:
        worker.shutdown()


def test_sweep_and_replacement(tmp_path):
    now = [0]
    worker = Supervisor(
        COMMANDS,
        max_sessions=2,
        idle_timeout_s=5,
        clock=lambda: now[0],
        parent_dir=tmp_path,
    )
    first = worker.create(OWNER, "mcp")
    now[0] = 5
    assert worker.sweep()[0].reason == "idle_timeout"
    second = worker.create(OWNER, "cli")
    reports = worker.shutdown()
    assert {r.reason for r in reports} == {"idle_timeout", "worker_replacement"}
    assert all(r.status == "ceased" for r in reports)
    assert not first.root.exists() and not second.root.exists()
    with pytest.raises(SessionRefused, match="shut down"):
        worker.create(OWNER, "mcp")


def test_partial_launch_cleanup(tmp_path):
    calls = []

    def launcher(command, **kwargs):
        if calls:
            raise OSError("second launch failed")
        process = launch(command, **kwargs)
        calls.append(process)
        return process

    worker = Supervisor(
        {arm: (COMMAND, COMMAND) for arm in COMMANDS},
        launcher=launcher,
        parent_dir=tmp_path,
    )
    with pytest.raises(LaunchFailed) as failure:
        worker.create(OWNER, "mcp")
    assert failure.value.report.status == "ceased"
    assert failure.value.report.reason == "launch_failure"
    assert calls[0].returncode is not None
    assert not list(tmp_path.iterdir())


def test_failed_cleanup_is_unconfirmed_and_holds_slot(tmp_path, monkeypatch):
    worker = Supervisor(COMMANDS, parent_dir=tmp_path)
    session = worker.create(OWNER, "mcp")
    with monkeypatch.context() as patch:
        patch.setattr(
            "session.shutil.rmtree",
            lambda _: (_ for _ in ()).throw(OSError("blocked cleanup")),
        )
        report = worker.terminate(session.session_id, OWNER, "complete")
        assert report.status == "unconfirmed" and not report.dirs_gone
        assert "cleanup" in report.errors[0]
        with pytest.raises(SessionRefused, match="admission"):
            worker.create(OWNER, "mcp")
    assert worker.shutdown()[0].status == "ceased"


def test_failed_reap_is_unconfirmed(tmp_path, monkeypatch):
    worker = Supervisor(COMMANDS, kill_grace_s=0, parent_dir=tmp_path)
    session = worker.create(OWNER, "cli")
    with monkeypatch.context() as patch:
        patch.setattr(
            session.processes[0],
            "wait",
            lambda **_: (_ for _ in ()).throw(subprocess.TimeoutExpired("test", 0)),
        )
        report = worker.terminate(session.session_id, OWNER, "complete")
        assert report.status == "unconfirmed" and not report.processes[0].reaped
    assert worker.shutdown()[0].status == "ceased"


@pytest.mark.parametrize("value", (None, "", 1, [], float("nan")))
def test_bad_owner_identity(value):
    with pytest.raises(ValueError):
        SessionOwner(value, "run", "principal")


@pytest.mark.parametrize("value", (None, "", 0, -1, float("nan"), True))
@pytest.mark.parametrize("field", ("max_sessions", "max_lifetime_s", "idle_timeout_s"))
def test_invalid_bounds(value, field):
    with pytest.raises(ValueError):
        Supervisor(COMMANDS, **{field: value})


@pytest.mark.parametrize("value", (None, "", -1, float("nan"), True))
def test_invalid_grace(value):
    with pytest.raises(ValueError):
        Supervisor(COMMANDS, kill_grace_s=value)


@pytest.mark.parametrize(
    "commands",
    (
        None,
        {},
        {"mcp": (COMMAND,)},
        {"mcp": (), "cli": (COMMAND,)},
        {"mcp": ((None,),), "cli": (COMMAND,)},
    ),
)
def test_invalid_commands(commands):
    with pytest.raises(ValueError):
        Supervisor(commands)


@pytest.mark.parametrize("bad", (None, "", [], 1))
def test_invalid_api_inputs(supervisor, bad):
    with pytest.raises(ValueError):
        supervisor.create(bad, "mcp")
    with pytest.raises(ValueError):
        supervisor.create(OWNER, bad)
    with pytest.raises(ValueError):
        supervisor.attach(bad, OWNER)
    with pytest.raises(ValueError):
        supervisor.terminate("missing", OWNER, bad)


def test_clock_reversal_refused_and_shutdown_still_cleans(tmp_path):
    now = [1]
    worker = Supervisor(COMMANDS, clock=lambda: now[0], parent_dir=tmp_path)
    session = worker.create(OWNER, "cli")
    now[0] = 0
    with pytest.raises(SessionRefused, match="backwards"):
        worker.attach(session.session_id, OWNER)
    report = worker.shutdown()[0]
    assert report.status == "unconfirmed" and report.dirs_gone
    assert report.processes[0].reaped
    now[0] = 2
    assert worker.shutdown()[0].status == "ceased"
