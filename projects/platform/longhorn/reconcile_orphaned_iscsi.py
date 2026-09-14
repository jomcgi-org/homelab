#!/usr/bin/env python3
"""Safely identify and optionally log out orphaned Longhorn iSCSI sessions."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol, Sequence


LONGHORN_TARGET_PREFIX = "iqn.2019-10.io.longhorn:"
VOLUME_NAME_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")
SESSION_RE = re.compile(
    r"^tcp: \[(?P<sid>[1-9][0-9]*)\] "
    r"(?P<portal>(?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+):"
    r"(?P<port>[1-9][0-9]{0,4}),(?P<tpgt>[0-9]+)) "
    r"(?P<target>\S+?)(?: \((?:non-flash|flash)\))?$"
)
SESSION_TYPE_SUFFIX_RE = re.compile(r" \((?:non-flash|flash)\)$")
ATTACHED_DISK_RE = re.compile(
    r"^Attached scsi disk (?P<device>[A-Za-z0-9._-]+)\s+State: (?P<state>\S+)$"
)
SUPPORTED_VOLUME_API_VERSIONS = frozenset(
    {"longhorn.io/v1beta1", "longhorn.io/v1beta2"}
)


class SafetyError(RuntimeError):
    """An uncertainty that requires the reconciler to fail closed."""


class CommandTimedOut(SafetyError):
    """A bounded subprocess exceeded its deadline."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Runner(Protocol):
    def run(self, args: Sequence[str]) -> CommandResult: ...


class SubprocessRunner:
    def __init__(self, timeout_seconds: int) -> None:
        self.timeout_seconds = timeout_seconds

    def run(self, args: Sequence[str]) -> CommandResult:
        try:
            completed = subprocess.run(
                list(args),
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandTimedOut(
                f"command timed out after {self.timeout_seconds}s: {args[0]}"
            ) from exc
        except OSError as exc:
            raise SafetyError(f"could not execute {args[0]}: {exc}") from exc
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True)
class Inventory:
    volume_names: frozenset[str]
    resource_version: str


@dataclass(frozen=True)
class Session:
    sid: int
    portal: str
    target: str
    volume_name: str | None

    @property
    def identity(self) -> tuple[int, str, str]:
        return (self.sid, self.portal, self.target)


@dataclass(frozen=True)
class SessionInspection:
    session: Session
    devices: tuple[str, ...]


@dataclass(frozen=True)
class Usage:
    in_use: bool
    reasons: tuple[str, ...] = ()


@dataclass
class ReconcileReport:
    sink: Callable[[str], None] | None = field(default=None, repr=False)
    lines: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    logout_attempts: list[int] = field(default_factory=list)
    logout_successes: list[int] = field(default_factory=list)

    def emit(self, line: str) -> None:
        self.lines.append(line)
        if self.sink is not None:
            self.sink(line)

    def fail(self, line: str) -> None:
        self.emit(f"MANUAL {line}")
        self.failures.append(line)


def _require_clean_success(result: CommandResult, operation: str) -> str:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise SafetyError(f"{operation} failed with exit {result.returncode}: {detail}")
    if result.stderr.strip():
        raise SafetyError(
            f"{operation} produced unexpected stderr: {result.stderr.strip()}"
        )
    return result.stdout


def parse_inventory(raw: str) -> Inventory:
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SafetyError(f"volume inventory is not valid JSON: {exc.msg}") from exc
    if not isinstance(document, dict):
        raise SafetyError("volume inventory root is not an object")
    if document.get("apiVersion") not in SUPPORTED_VOLUME_API_VERSIONS:
        raise SafetyError("volume inventory has an unsupported apiVersion")
    if document.get("kind") != "VolumeList":
        raise SafetyError("volume inventory is not a VolumeList")
    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise SafetyError("volume inventory metadata is missing")
    resource_version = metadata.get("resourceVersion")
    if not isinstance(resource_version, str) or not resource_version:
        raise SafetyError("volume inventory resourceVersion is missing")
    if metadata.get("continue") not in (None, ""):
        raise SafetyError("volume inventory is paginated and therefore incomplete")
    if metadata.get("remainingItemCount") not in (None, 0):
        raise SafetyError("volume inventory reports remaining items")
    items = document.get("items")
    if not isinstance(items, list):
        raise SafetyError("volume inventory items are missing")

    names: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise SafetyError(f"volume inventory item {index} is not an object")
        if item.get("apiVersion") not in SUPPORTED_VOLUME_API_VERSIONS:
            raise SafetyError(
                f"volume inventory item {index} has an unsupported apiVersion"
            )
        if item.get("kind") != "Volume":
            raise SafetyError(f"volume inventory item {index} is not a Volume")
        item_metadata = item.get("metadata")
        if not isinstance(item_metadata, dict):
            raise SafetyError(f"volume inventory item {index} metadata is missing")
        name = item_metadata.get("name")
        if not isinstance(name, str) or not VOLUME_NAME_RE.fullmatch(name):
            raise SafetyError(f"volume inventory item {index} has an invalid name")
        if name in names:
            raise SafetyError(f"volume inventory contains duplicate volume {name}")
        names.add(name)
    return Inventory(frozenset(names), resource_version)


def parse_sessions(raw: str) -> tuple[Session, ...]:
    sessions: list[Session] = []
    seen_sids: set[int] = set()
    seen_longhorn_volumes: set[str] = set()
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        match = SESSION_RE.fullmatch(line)
        if match is None:
            raise SafetyError(f"unrecognized iSCSI session line {line_number}: {line}")
        port = int(match.group("port"))
        if port > 65535:
            raise SafetyError(f"iSCSI session line {line_number} has an invalid port")
        sid = int(match.group("sid"))
        if sid in seen_sids:
            raise SafetyError(f"iSCSI session id {sid} appears more than once")
        seen_sids.add(sid)
        target = match.group("target")
        volume_name: str | None = None
        if target.startswith(LONGHORN_TARGET_PREFIX):
            candidate = target.removeprefix(LONGHORN_TARGET_PREFIX)
            if not VOLUME_NAME_RE.fullmatch(candidate):
                raise SafetyError(
                    f"Longhorn session {sid} has an invalid volume target"
                )
            if candidate in seen_longhorn_volumes:
                raise SafetyError(
                    f"Longhorn volume {candidate} has multiple local iSCSI sessions"
                )
            seen_longhorn_volumes.add(candidate)
            volume_name = candidate
        sessions.append(
            Session(
                sid=sid,
                portal=match.group("portal"),
                target=target,
                volume_name=volume_name,
            )
        )
    return tuple(sessions)


def parse_session_inspection(session: Session, raw: str) -> SessionInspection:
    targets: list[str] = []
    portals: list[str] = []
    sids: list[int] = []
    devices: list[str] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if line.startswith("Target: "):
            target = line.removeprefix("Target: ")
            targets.append(SESSION_TYPE_SUFFIX_RE.sub("", target))
        elif line.startswith("Persistent Portal: "):
            portals.append(line.removeprefix("Persistent Portal: "))
        elif line.startswith("SID: "):
            value = line.removeprefix("SID: ")
            if not value.isdigit():
                raise SafetyError(
                    f"session {session.sid} inspection has an invalid SID"
                )
            sids.append(int(value))
        elif "Attached scsi disk" in line:
            match = ATTACHED_DISK_RE.fullmatch(line)
            if match is None:
                raise SafetyError(
                    f"session {session.sid} inspection has an ambiguous attached device"
                )
            devices.append(match.group("device"))
    if targets != [session.target]:
        raise SafetyError(f"session {session.sid} target changed during inspection")
    if portals != [session.portal]:
        raise SafetyError(f"session {session.sid} portal changed during inspection")
    if sids != [session.sid]:
        raise SafetyError(f"session {session.sid} identity changed during inspection")
    if len(devices) != len(set(devices)):
        raise SafetyError(
            f"session {session.sid} inspection repeats an attached device"
        )
    return SessionInspection(session, tuple(devices))


def parse_lsblk(raw: str, requested_device: str) -> tuple[str, ...]:
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SafetyError(
            f"lsblk output for {requested_device} is not valid JSON"
        ) from exc
    if not isinstance(document, dict) or not isinstance(
        document.get("blockdevices"), list
    ):
        raise SafetyError(f"lsblk output for {requested_device} is malformed")
    paths: list[str] = []
    mounted: list[str] = []

    def visit(node: object) -> None:
        if not isinstance(node, dict):
            raise SafetyError(
                f"lsblk output for {requested_device} has a malformed device"
            )
        name = node.get("name")
        if not isinstance(name, str) or not name.startswith("/dev/"):
            raise SafetyError(
                f"lsblk output for {requested_device} has an invalid path"
            )
        paths.append(name)
        mountpoints = node.get("mountpoints")
        if not isinstance(mountpoints, list):
            raise SafetyError(f"lsblk output for {requested_device} omits mountpoints")
        for mountpoint in mountpoints:
            if mountpoint is not None and mountpoint != "":
                if not isinstance(mountpoint, str):
                    raise SafetyError(
                        f"lsblk output for {requested_device} has an invalid mountpoint"
                    )
                mounted.append(f"{name} mounted at {mountpoint}")
        children = node.get("children", [])
        if not isinstance(children, list):
            raise SafetyError(
                f"lsblk output for {requested_device} has invalid children"
            )
        for child in children:
            visit(child)

    for block_device in document["blockdevices"]:
        visit(block_device)
    expected_path = f"/dev/{requested_device}"
    if expected_path not in paths:
        raise SafetyError(f"lsblk did not return requested device {expected_path}")
    if len(paths) != len(set(paths)):
        raise SafetyError(f"lsblk repeated a path for {requested_device}")
    if mounted:
        raise SafetyError("; ".join(mounted))
    return tuple(paths)


class InventoryClient:
    def __init__(
        self,
        runner: Runner,
        context: str,
        namespace: str,
        timeout_seconds: int,
        allow_empty: bool,
    ) -> None:
        self.runner = runner
        self.context = context
        self.namespace = namespace
        self.timeout_seconds = timeout_seconds
        self.allow_empty = allow_empty

    def _read_once(self) -> Inventory:
        result = self.runner.run(
            [
                "kubectl",
                "--context",
                self.context,
                "--namespace",
                self.namespace,
                "get",
                "volumes.longhorn.io",
                "--output=json",
                "--chunk-size=0",
                f"--request-timeout={self.timeout_seconds}s",
            ]
        )
        return parse_inventory(
            _require_clean_success(result, "Longhorn volume inventory")
        )

    def authoritative(self) -> Inventory:
        inventory = self._read_once()
        if inventory.volume_names:
            return inventory
        if not self.allow_empty:
            raise SafetyError(
                "Longhorn volume inventory is empty; use --allow-empty-inventory only "
                "after independently confirming the cluster has no volumes"
            )
        confirmation = self._read_once()
        if confirmation.volume_names:
            raise SafetyError(
                "Longhorn volume inventory changed while confirming emptiness"
            )
        return confirmation


class IscsiClient:
    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    def sessions(self) -> tuple[Session, ...]:
        result = self.runner.run(["iscsiadm", "-m", "session"])
        if result.returncode == 21:
            combined = "\n".join(
                part.strip() for part in (result.stdout, result.stderr) if part.strip()
            )
            if combined in {"No active sessions.", "iscsiadm: No active sessions."}:
                return ()
        raw = _require_clean_success(result, "iSCSI session inventory")
        return parse_sessions(raw)

    def inspect(self, session: Session) -> SessionInspection:
        result = self.runner.run(
            ["iscsiadm", "-m", "session", "-r", str(session.sid), "-P", "3"]
        )
        raw = _require_clean_success(
            result, f"inspection of iSCSI session {session.sid}"
        )
        return parse_session_inspection(session, raw)

    def logout(self, session: Session) -> CommandResult:
        return self.runner.run(
            ["iscsiadm", "-m", "session", "-r", str(session.sid), "--logout"]
        )


class SystemUsageChecker:
    def __init__(
        self, runner: Runner, sysfs_root: Path = Path("/sys/class/block")
    ) -> None:
        self.runner = runner
        self.sysfs_root = sysfs_root

    def check(self, inspection: SessionInspection) -> Usage:
        if not inspection.devices:
            return Usage(False)
        all_paths: set[str] = set()
        reasons: list[str] = []
        for device in inspection.devices:
            result = self.runner.run(
                [
                    "lsblk",
                    "--json",
                    "--paths",
                    "--output",
                    "NAME,MOUNTPOINTS",
                    f"/dev/{device}",
                ]
            )
            raw = _require_clean_success(result, f"lsblk inspection of /dev/{device}")
            try:
                paths = parse_lsblk(raw, device)
            except SafetyError as exc:
                if " mounted at " in str(exc):
                    reasons.append(str(exc))
                    continue
                raise
            all_paths.update(paths)

        swap_result = self.runner.run(
            ["swapon", "--show=NAME", "--noheadings", "--raw"]
        )
        swap_raw = _require_clean_success(swap_result, "swap inventory")
        swap_paths = {line.strip() for line in swap_raw.splitlines() if line.strip()}
        if any(not path.startswith("/dev/") for path in swap_paths):
            raise SafetyError("swap inventory contains an invalid device path")

        for device_path in sorted(all_paths):
            if device_path in swap_paths:
                reasons.append(f"{device_path} is active swap")
            device_name = Path(device_path).name
            holders_path = self.sysfs_root / device_name / "holders"
            try:
                holders = sorted(entry.name for entry in holders_path.iterdir())
            except OSError as exc:
                raise SafetyError(
                    f"cannot inspect holders for {device_path}: {exc}"
                ) from exc
            if holders:
                reasons.append(f"{device_path} has holders: {','.join(holders)}")
            fuser_result = self.runner.run(["fuser", "--", device_path])
            if fuser_result.returncode == 0:
                reasons.append(f"{device_path} has open users")
            elif fuser_result.returncode == 1:
                if fuser_result.stdout.strip() or fuser_result.stderr.strip():
                    raise SafetyError(
                        f"fuser returned ambiguous output for {device_path}"
                    )
            else:
                detail = (
                    fuser_result.stderr.strip()
                    or fuser_result.stdout.strip()
                    or "no output"
                )
                raise SafetyError(
                    f"fuser inspection of {device_path} failed with exit "
                    f"{fuser_result.returncode}: {detail}"
                )
        return Usage(bool(reasons), tuple(reasons))


class UsageChecker(Protocol):
    def check(self, inspection: SessionInspection) -> Usage: ...


class Reconciler:
    def __init__(
        self,
        inventory: InventoryClient,
        iscsi: IscsiClient,
        usage: UsageChecker,
        apply: bool,
    ) -> None:
        self.inventory = inventory
        self.iscsi = iscsi
        self.usage = usage
        self.apply = apply

    @staticmethod
    def _by_sid(sessions: Iterable[Session]) -> dict[int, Session]:
        return {session.sid: session for session in sessions}

    def _safe_candidate(
        self, session: Session, report: ReconcileReport
    ) -> SessionInspection | None:
        try:
            inspection = self.iscsi.inspect(session)
            usage = self.usage.check(inspection)
        except SafetyError as exc:
            report.fail(f"session={session.sid} volume={session.volume_name}: {exc}")
            return None
        if usage.in_use:
            report.fail(
                f"session={session.sid} volume={session.volume_name} is in use: "
                + "; ".join(usage.reasons)
            )
            return None
        return inspection

    def _apply_candidate(self, original: Session, report: ReconcileReport) -> None:
        if original.volume_name is None:
            report.fail(f"session={original.sid}: non-Longhorn candidate rejected")
            return
        try:
            inventory = self.inventory.authoritative()
            if original.volume_name in inventory.volume_names:
                report.emit(
                    f"PROTECTED session={original.sid} volume={original.volume_name} "
                    "appeared in the current inventory"
                )
                return
            current = self._by_sid(self.iscsi.sessions()).get(original.sid)
            if current is None:
                report.emit(
                    f"SKIPPED session={original.sid} volume={original.volume_name} no longer exists"
                )
                return
            if current.identity != original.identity:
                raise SafetyError("session identity changed before logout")
            inspection = self.iscsi.inspect(current)
            usage = self.usage.check(inspection)
            if usage.in_use:
                raise SafetyError("session became in use: " + "; ".join(usage.reasons))

            final_inventory = self.inventory.authoritative()
            if original.volume_name in final_inventory.volume_names:
                report.emit(
                    f"PROTECTED session={original.sid} volume={original.volume_name} "
                    "appeared during final revalidation"
                )
                return
            final_session = self._by_sid(self.iscsi.sessions()).get(original.sid)
            if final_session is None:
                report.emit(
                    f"SKIPPED session={original.sid} volume={original.volume_name} "
                    "disappeared during final revalidation"
                )
                return
            if final_session.identity != original.identity:
                raise SafetyError("session identity changed during final revalidation")

            report.emit(
                f"LOGOUT_ATTEMPT session={original.sid} volume={original.volume_name} "
                f"target={original.target} portal={original.portal}"
            )
            report.logout_attempts.append(original.sid)
            logout = self.iscsi.logout(final_session)
            if logout.returncode != 0:
                detail = logout.stderr.strip() or logout.stdout.strip() or "no output"
                raise SafetyError(
                    f"targeted logout failed with exit {logout.returncode}: {detail}"
                )
            report.emit(
                f"LOGOUT_RETURNED_SUCCESS session={original.sid} "
                f"volume={original.volume_name} verification=pending"
            )
            remaining = self._by_sid(self.iscsi.sessions()).get(original.sid)
            if remaining is not None and remaining.identity == original.identity:
                raise SafetyError(
                    "targeted logout returned success but the session remains"
                )
            report.logout_successes.append(original.sid)
            report.emit(
                f"LOGGED_OUT session={original.sid} volume={original.volume_name} "
                f"target={original.target} portal={original.portal}"
            )
        except SafetyError as exc:
            report.fail(f"session={original.sid} volume={original.volume_name}: {exc}")

    def run(self, report: ReconcileReport | None = None) -> ReconcileReport:
        if report is None:
            report = ReconcileReport()
        initial_inventory = self.inventory.authoritative()
        sessions = self.iscsi.sessions()
        candidates: list[Session] = []
        for session in sessions:
            if session.volume_name is None:
                report.emit(
                    f"IGNORED session={session.sid} non-Longhorn target={session.target}"
                )
                continue
            if session.volume_name in initial_inventory.volume_names:
                report.emit(
                    f"PROTECTED session={session.sid} volume={session.volume_name} "
                    "exists in Longhorn inventory"
                )
                continue
            if self._safe_candidate(session, report) is None:
                continue
            candidates.append(session)
            report.emit(
                f"CANDIDATE session={session.sid} volume={session.volume_name} "
                f"target={session.target} portal={session.portal}"
            )

        if not self.apply:
            report.emit(
                f"DRY_RUN candidates={len(candidates)} logout_attempts=0; "
                "rerun with --apply to revalidate and log out each candidate"
            )
            return report
        for candidate in candidates:
            self._apply_candidate(candidate, report)
        report.emit(
            f"APPLY_DONE candidates={len(candidates)} "
            f"logout_attempts={len(report.logout_attempts)} "
            f"logout_successes={len(report.logout_successes)} "
            f"manual={len(report.failures)}"
        )
        return report


def validate_node(
    runner: Runner, context: str, node: str, timeout_seconds: int
) -> None:
    hostname_result = runner.run(["hostname", "--short"])
    hostname = _require_clean_success(hostname_result, "local hostname").strip()
    if hostname != node:
        raise SafetyError(f"local hostname {hostname!r} does not match --node {node!r}")
    result = runner.run(
        [
            "kubectl",
            "--context",
            context,
            "get",
            "node",
            node,
            "--output=json",
            f"--request-timeout={timeout_seconds}s",
        ]
    )
    raw = _require_clean_success(result, f"Kubernetes node {node}")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SafetyError(f"Kubernetes node {node} is not valid JSON") from exc
    metadata = document.get("metadata") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("apiVersion") != "v1"
        or document.get("kind") != "Node"
        or not isinstance(metadata, dict)
        or metadata.get("name") != node
        or not isinstance(metadata.get("uid"), str)
        or not metadata.get("uid")
        or metadata.get("deletionTimestamp") is not None
    ):
        raise SafetyError(
            f"Kubernetes node {node} response is incomplete or mismatched"
        )


class RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "RunLock":
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            self.fd = os.open(self.path, flags, 0o600)
            file_stat = os.fstat(self.fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise SafetyError(f"lock path is not a regular file: {self.path}")
            if file_stat.st_uid != os.geteuid():
                raise SafetyError(f"lock file is owned by another user: {self.path}")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.close()
            raise SafetyError(
                f"another reconciler is already running: {self.path}"
            ) from exc
        except OSError as exc:
            self.close()
            raise SafetyError(
                f"cannot acquire reconciler lock {self.path}: {exc}"
            ) from exc
        return self

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True, help="exact kubectl context")
    parser.add_argument(
        "--node", required=True, help="exact local Kubernetes node name"
    )
    parser.add_argument("--namespace", default="longhorn")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform targeted logouts after all per-session revalidation",
    )
    parser.add_argument(
        "--allow-empty-inventory",
        action="store_true",
        help="accept two consecutive complete empty Longhorn volume inventories",
    )
    parser.add_argument("--timeout-seconds", type=int, default=15)
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=Path("/run/lock/longhorn-iscsi-reconcile.lock"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.timeout_seconds <= 60:
        print("FATAL --timeout-seconds must be between 1 and 60", file=sys.stderr)
        return 2
    runner = SubprocessRunner(args.timeout_seconds)
    report = ReconcileReport(sink=lambda line: print(line, flush=True))
    try:
        with RunLock(args.lock_file):
            validate_node(runner, args.context, args.node, args.timeout_seconds)
            inventory = InventoryClient(
                runner,
                args.context,
                args.namespace,
                args.timeout_seconds,
                args.allow_empty_inventory,
            )
            iscsi = IscsiClient(runner)
            report = Reconciler(
                inventory,
                iscsi,
                SystemUsageChecker(runner),
                args.apply,
            ).run(report)
    except SafetyError as exc:
        print(f"FATAL {exc}", file=sys.stderr)
        return 2
    return 2 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
