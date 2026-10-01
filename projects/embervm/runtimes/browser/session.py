"""Bounded process supervisor for a planned, separate EmberVM browser guest.

Launchers are trusted adapters and must return Popen-compatible handles after
honouring start_new_session=True. Command templates are supplied by the caller.
No provider, network, guest provisioning or browser authorization lives here.
"""

import math
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ARMS = frozenset(("mcp", "cli"))
TERMINATION_REASONS = frozenset(
    (
        "complete",
        "cancel",
        "idle_timeout",
        "max_lifetime",
        "worker_replacement",
        "launch_failure",
    )
)


def _number(value, name, *, zero=False):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or value < 0
        or (not zero and value == 0)
    ):
        raise ValueError(
            name + " must be finite and " + ("nonnegative" if zero else "positive")
        )
    return value


def _identity(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("identity must be a nonempty string")
    return value


@dataclass(frozen=True)
class SessionOwner:
    task_id: str
    run_id: str
    principal: str

    def __post_init__(self):
        for value in (self.task_id, self.run_id, self.principal):
            _identity(value)


@dataclass(frozen=True)
class ProcessExit:
    pid: int
    exit_code: int | None
    signal: int | None
    reaped: bool
    group_gone: bool


@dataclass(frozen=True)
class CessationReport:
    session_id: str
    reason: str
    status: str
    processes: tuple[ProcessExit, ...]
    dirs_gone: bool
    started_monotonic: float
    finished_monotonic: float
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class BrowserSession:
    session_id: str
    owner: SessionOwner
    arm: str
    root: Path
    profile_dir: Path
    evidence_dir: Path
    created_monotonic: float
    last_activity_monotonic: float
    processes: tuple = field(default_factory=tuple)
    termination_reason: str | None = None
    report: CessationReport | None = None


class SessionRefused(ValueError):
    """Admission, attachment or termination was refused explicitly."""


class LaunchFailed(SessionRefused):
    def __init__(self, report):
        self.report = report
        super().__init__("launch failed; cessation " + report.status)


def launch(command, **kwargs):
    """Start a command in its own process group without shell interpolation."""
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )


class Supervisor:
    def __init__(
        self,
        commands,
        *,
        max_sessions=1,
        max_lifetime_s=900,
        idle_timeout_s=120,
        kill_grace_s=1,
        launcher=launch,
        clock=time.monotonic,
        parent_dir=None,
    ):
        if type(max_sessions) is not int or max_sessions <= 0:
            raise ValueError("max_sessions must be a positive integer")
        self.max_sessions = max_sessions
        self.max_lifetime_s = _number(max_lifetime_s, "max_lifetime_s")
        self.idle_timeout_s = _number(idle_timeout_s, "idle_timeout_s")
        self.kill_grace_s = _number(kill_grace_s, "kill_grace_s", zero=True)
        if not callable(launcher) or not callable(clock):
            raise ValueError("launcher and clock must be callable")
        if not isinstance(commands, dict) or set(commands) != ARMS:
            raise ValueError("command templates are required for both arms")
        for arm_commands in commands.values():
            if not isinstance(arm_commands, tuple) or not arm_commands:
                raise ValueError("each arm requires a nonempty tuple of commands")
            for command in arm_commands:
                if (
                    not isinstance(command, tuple)
                    or not command
                    or any(not isinstance(v, str) or not v for v in command)
                ):
                    raise ValueError("each command requires nonempty string argv")
        self.commands = dict(commands)
        self.launcher, self.clock, self.parent_dir = launcher, clock, parent_dir
        self._sessions = {}
        self._lock = threading.RLock()
        self._closed = False
        self._last_clock = None
        self._now()

    def _now(self):
        value = _number(self.clock(), "monotonic clock", zero=True)
        if self._last_clock is not None and value < self._last_clock:
            raise SessionRefused("monotonic clock moved backwards")
        self._last_clock = value
        return value

    def _get(self, session_id, owner):
        if not isinstance(owner, SessionOwner):
            raise SessionRefused("session owner is required")
        owner.__post_init__()
        _identity(session_id)
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionRefused("unknown session")
        # A run is also fixed: retries cannot adopt another run's browser state.
        if owner != session.owner:
            raise SessionRefused("task, run or principal mismatch")
        return session

    def _expiry(self, session, now):
        if now - session.created_monotonic >= self.max_lifetime_s:
            return "max_lifetime"
        if now - session.last_activity_monotonic >= self.idle_timeout_s:
            return "idle_timeout"
        return None

    def create(self, owner, arm):
        with self._lock:
            if not isinstance(owner, SessionOwner):
                raise SessionRefused("session owner is required")
            owner.__post_init__()
            if not isinstance(arm, str) or arm not in ARMS:
                raise SessionRefused("unknown arm")
            if self._closed:
                raise SessionRefused("worker is shut down")
            self.sweep()
            # An unconfirmed cessation retains its admission slot.
            if (
                sum(
                    s.report is None or s.report.status != "ceased"
                    for s in self._sessions.values()
                )
                >= self.max_sessions
            ):
                raise SessionRefused("session admission limit reached")
            now = self._now()
            root = Path(
                tempfile.mkdtemp(prefix="browser-session-", dir=self.parent_dir)
            )
            os.chmod(root, 0o700)
            session_id = uuid.uuid4().hex
            session = BrowserSession(
                session_id,
                owner,
                arm,
                root,
                root / "profile",
                root / "evidence",
                now,
                now,
            )
            self._sessions[session_id] = session
            try:
                session.profile_dir.mkdir(mode=0o700)
                session.evidence_dir.mkdir(mode=0o700)
                for template in self.commands[arm]:
                    command = tuple(
                        arg.replace("{profile_dir}", str(session.profile_dir))
                        .replace("{evidence_dir}", str(session.evidence_dir))
                        .replace("{session_id}", session_id)
                        for arg in template
                    )
                    process = self.launcher(
                        command, cwd=str(root), start_new_session=True
                    )
                    if not isinstance(process, subprocess.Popen):
                        raise SessionRefused("launcher did not return a process handle")
                    object.__setattr__(
                        session, "processes", session.processes + (process,)
                    )
                    if os.getpgid(process.pid) != process.pid:
                        raise SessionRefused(
                            "launcher did not isolate the process group"
                        )
            except Exception:
                raise LaunchFailed(self._terminate(session, "launch_failure")) from None
            return session

    def attach(self, session_id, owner):
        with self._lock:
            session = self._get(session_id, owner)
            if self._closed or session.termination_reason is not None:
                raise SessionRefused("session is terminated")
            now = self._now()
            expired = self._expiry(session, now)
            if expired:
                self._terminate(session, expired)
                raise SessionRefused("session expired: " + expired)
            if any(p.poll() is not None for p in session.processes):
                self._terminate(session, "cancel")
                raise SessionRefused("session process exited")
            object.__setattr__(session, "last_activity_monotonic", now)
            return session

    def terminate(self, session_id, owner, reason):
        with self._lock:
            if not isinstance(reason, str) or reason not in ("complete", "cancel"):
                raise SessionRefused(
                    "caller termination reason must be complete or cancel"
                )
            session = self._get(session_id, owner)
            return self._terminate(
                session, self._expiry(session, self._now()) or reason
            )

    def _terminate(self, session, reason):
        if session.report is not None and session.report.status == "ceased":
            return session.report
        errors, exits = [], []
        object.__setattr__(
            session, "termination_reason", session.termination_reason or reason
        )

        def cessation_time():
            try:
                return self._now()
            except (ValueError, TypeError) as error:
                errors.append("clock: " + str(error))
                return self._last_clock

        started = cessation_time()
        # Signal all groups before waiting on any one process.
        for process in session.processes:
            try:
                if os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
            except ProcessLookupError:
                pass
            except OSError as error:
                errors.append("SIGTERM: " + str(error))
        for process in session.processes:
            code, reaped, group_gone = None, False, False
            try:
                try:
                    code = process.wait(timeout=self.kill_grace_s)
                except subprocess.TimeoutExpired:
                    try:
                        if os.getpgid(process.pid) == process.pid:
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                    except ProcessLookupError:
                        pass
                    code = process.wait(timeout=max(self.kill_grace_s, 0.1))
                reaped = type(code) is int
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    group_gone = True
                else:
                    # A leader can exit while a child still owns the group.
                    os.killpg(process.pid, signal.SIGKILL)
                    errors.append("process group still present after leader reap")
            except (OSError, subprocess.TimeoutExpired) as error:
                errors.append("reap: " + str(error))
            exits.append(
                ProcessExit(
                    process.pid,
                    code if code is not None and code >= 0 else None,
                    -code if code is not None and code < 0 else None,
                    reaped,
                    group_gone,
                )
            )
        try:
            shutil.rmtree(session.root)
        except FileNotFoundError:
            pass
        except OSError as error:
            errors.append("cleanup: " + str(error))
        gone = not os.path.lexists(session.root)
        finished = cessation_time()
        confirmed = (
            gone and all(p.reaped and p.group_gone for p in exits) and not errors
        )
        object.__setattr__(
            session,
            "report",
            CessationReport(
                session.session_id,
                session.termination_reason,
                "ceased" if confirmed else "unconfirmed",
                tuple(exits),
                gone,
                started,
                finished,
                tuple(errors),
            ),
        )
        return session.report

    def sweep(self):
        """Enforce idle and lifetime expiry; the future worker must call regularly."""
        with self._lock:
            now = self._now()
            return tuple(
                self._terminate(session, reason)
                for session in self._sessions.values()
                if session.termination_reason is None
                and (reason := self._expiry(session, now))
            )

    def shutdown(self):
        """Worker replacement refuses new sessions and terminates every old one."""
        with self._lock:
            self._closed = True
            return tuple(
                self._terminate(session, "worker_replacement")
                for session in self._sessions.values()
            )
