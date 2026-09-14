from __future__ import annotations

import json

import pytest

from reconcile_orphaned_iscsi import (
    CommandResult,
    CommandTimedOut,
    DeviceMounted,
    Inventory,
    InventoryClient,
    IscsiClient,
    ReconcileReport,
    Reconciler,
    SafetyError,
    Session,
    SessionInspection,
    Usage,
    build_parser,
    parse_inventory,
    parse_lsblk,
    parse_session_inspection,
    parse_sessions,
)


LONGHORN_PREFIX = "iqn.2019-10.io.longhorn:"

# Captured from `kubectl get volumes.longhorn.io --output=json --chunk-size=0`.
# kubectl flattens the server's VolumeList into a core v1 List and drops its
# list metadata, so this shape cannot prove that an absence is authoritative.
KUBECTL_GENERIC_LIST_CAPTURE = json.dumps(
    {
        "apiVersion": "v1",
        "items": [
            {
                "apiVersion": "longhorn.io/v1beta2",
                "kind": "Volume",
                "metadata": {"name": "pvc-live"},
            },
            {
                "apiVersion": "longhorn.io/v1beta2",
                "kind": "Volume",
                "metadata": {"name": "pvc-other"},
            },
        ],
        "kind": "List",
        "metadata": {"resourceVersion": ""},
    }
)


def volume_document(*names: str, metadata: dict[str, object] | None = None) -> str:
    return json.dumps(
        {
            "apiVersion": "longhorn.io/v1beta2",
            "kind": "VolumeList",
            "metadata": metadata or {"resourceVersion": "101"},
            "items": [
                {
                    "apiVersion": "longhorn.io/v1beta2",
                    "kind": "Volume",
                    "metadata": {"name": name},
                }
                for name in names
            ],
        }
    )


def longhorn_session(
    sid: int, volume: str, portal: str = "10.42.0.90:3260,1"
) -> Session:
    return Session(sid, portal, f"{LONGHORN_PREFIX}{volume}", volume)


class QueueRunner:
    def __init__(self, results: list[CommandResult | Exception]) -> None:
        self.results = results
        self.commands: list[tuple[str, ...]] = []

    def run(self, args: list[str]) -> CommandResult:
        self.commands.append(tuple(args))
        if not self.results:
            raise AssertionError(f"unexpected command: {args}")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeInventory:
    def __init__(self, volume_sets: list[set[str]]) -> None:
        self.volume_sets = volume_sets
        self.reads = 0

    def authoritative(self) -> Inventory:
        self.reads += 1
        if not self.volume_sets:
            raise AssertionError("unexpected inventory read")
        return Inventory(frozenset(self.volume_sets.pop(0)))


class FakeIscsi:
    def __init__(
        self,
        session_reads: list[tuple[Session, ...]],
        devices: dict[int, tuple[str, ...]] | None = None,
        logout_result: CommandResult | Exception = CommandResult(0),
    ) -> None:
        self.session_reads = session_reads
        self.devices = devices or {}
        self.logout_result = logout_result
        self.inspections: list[int] = []
        self.logouts: list[int] = []

    def sessions(self) -> tuple[Session, ...]:
        if not self.session_reads:
            raise AssertionError("unexpected session inventory read")
        return self.session_reads.pop(0)

    def inspect(self, session: Session) -> SessionInspection:
        self.inspections.append(session.sid)
        return SessionInspection(session, self.devices.get(session.sid, ()))

    def logout(self, session: Session) -> CommandResult:
        self.logouts.append(session.sid)
        if isinstance(self.logout_result, Exception):
            raise self.logout_result
        return self.logout_result


class FakeUsage:
    def __init__(self, responses: dict[int, Usage] | None = None) -> None:
        self.responses = responses or {}
        self.checks: list[int] = []

    def check(self, inspection: SessionInspection) -> Usage:
        self.checks.append(inspection.session.sid)
        return self.responses.get(inspection.session.sid, Usage(False))


@pytest.mark.parametrize(
    "raw, message",
    [
        ("not json", "not valid JSON"),
        (
            json.dumps(
                {
                    "apiVersion": "longhorn.io/v1beta2",
                    "kind": "VolumeList",
                    "metadata": {"resourceVersion": "1", "continue": "next"},
                    "items": [],
                }
            ),
            "incomplete",
        ),
        (
            json.dumps(
                {
                    "apiVersion": "longhorn.io/v1beta2",
                    "kind": "VolumeList",
                    "metadata": {"resourceVersion": "1"},
                    "items": [{"metadata": {"name": "pvc-a"}}],
                }
            ),
            "unsupported apiVersion",
        ),
    ],
)
def test_inventory_rejects_malformed_and_incomplete_responses(
    raw: str, message: str
) -> None:
    with pytest.raises(SafetyError, match=message):
        parse_inventory(raw)


def test_inventory_command_failure_fails_closed() -> None:
    runner = QueueRunner([CommandResult(1, stderr="forbidden")])
    client = InventoryClient(runner, "home", "longhorn", 15, False)

    with pytest.raises(SafetyError, match="forbidden"):
        client.authoritative()


def test_inventory_rejects_captured_kubectl_generic_list() -> None:
    with pytest.raises(SafetyError, match="unsupported apiVersion"):
        parse_inventory(KUBECTL_GENERIC_LIST_CAPTURE)


def test_inventory_read_uses_selected_context_and_namespace() -> None:
    runner = QueueRunner([CommandResult(0, volume_document("pvc-live"))])
    client = InventoryClient(runner, "home-prod", "longhorn", 23, False)

    assert client._read_once().volume_names == frozenset({"pvc-live"})
    assert runner.commands == [
        (
            "kubectl",
            "--context",
            "home-prod",
            "get",
            "--raw",
            "/apis/longhorn.io/v1beta2/namespaces/longhorn/volumes?limit=0",
            "--request-timeout=23s",
        )
    ]


def test_lsblk_reports_mounts_with_a_distinct_exception() -> None:
    raw = json.dumps(
        {
            "blockdevices": [
                {"name": "/dev/sdc", "mountpoints": ["/data"]},
            ]
        }
    )

    with pytest.raises(DeviceMounted, match="/dev/sdc mounted at /data"):
        parse_lsblk(raw, "sdc")


def test_parser_defaults_to_deployed_longhorn_namespace() -> None:
    args = build_parser().parse_args(["--context", "home", "--node", "node-4"])

    assert args.namespace == "longhorn"


def test_empty_inventory_requires_opt_in_and_two_complete_reads() -> None:
    rejected_runner = QueueRunner([CommandResult(0, volume_document())])
    rejected = InventoryClient(rejected_runner, "home", "longhorn", 15, False)
    with pytest.raises(SafetyError, match="--allow-empty-inventory"):
        rejected.authoritative()

    accepted_runner = QueueRunner(
        [CommandResult(0, volume_document()), CommandResult(0, volume_document())]
    )
    accepted = InventoryClient(accepted_runner, "home", "longhorn", 15, True)
    assert accepted.authoritative().volume_names == frozenset()
    assert len(accepted_runner.commands) == 2


def test_empty_inventory_confirmation_rejects_a_new_volume() -> None:
    runner = QueueRunner(
        [
            CommandResult(0, volume_document()),
            CommandResult(0, volume_document("pvc-new")),
        ]
    )
    client = InventoryClient(runner, "home", "longhorn", 15, True)

    with pytest.raises(SafetyError, match="changed while confirming"):
        client.authoritative()


def test_session_parser_rejects_ambiguous_longhorn_and_accepts_non_longhorn() -> None:
    parsed = parse_sessions(
        "tcp: [8] 10.0.0.8:3260,1 iqn.2020-01.example:database (flash)\n"
    )
    assert parsed[0].volume_name is None

    with pytest.raises(SafetyError, match="invalid volume target"):
        parse_sessions(
            "tcp: [9] 10.0.0.9:3260,1 iqn.2019-10.io.longhorn:PVC_BAD (non-flash)\n"
        )
    with pytest.raises(SafetyError, match="unrecognized"):
        parse_sessions("garbled output that might hide a session\n")


def test_session_inspection_requires_unchanged_identity() -> None:
    session = longhorn_session(14, "pvc-orphan", "10.42.0.230:3260,1")
    raw = """
Target: iqn.2019-10.io.longhorn:pvc-orphan (non-flash)
    Current Portal: 10.42.0.231:3260,1
    Persistent Portal: 10.42.0.230:3260,1
        SID: 14
        Attached scsi disk sdc          State: transport-offline
"""
    inspection = parse_session_inspection(session, raw)
    assert inspection.devices == ("sdc",)

    with pytest.raises(SafetyError, match="portal changed"):
        parse_session_inspection(
            session,
            raw.replace(
                "Persistent Portal: 10.42.0.230",
                "Persistent Portal: 10.42.0.90",
            ),
        )


def test_dry_run_classifies_mixed_sessions_without_mutation() -> None:
    live = longhorn_session(112, "pvc-live")
    orphan = longhorn_session(14, "pvc-orphan", "10.42.0.230:3260,1")
    other = Session(7, "10.0.0.7:3260,1", "iqn.2020-01.example:database", None)
    iscsi = FakeIscsi([(live, orphan, other)])

    report = Reconciler(
        FakeInventory([{"pvc-live"}]), iscsi, FakeUsage(), apply=False
    ).run()

    assert iscsi.logouts == []
    assert iscsi.inspections == [14]
    assert any(line.startswith("PROTECTED session=112") for line in report.lines)
    assert any(line.startswith("CANDIDATE session=14") for line in report.lines)
    assert any(line.startswith("IGNORED session=7") for line in report.lines)
    assert report.lines[-1].startswith("DRY_RUN candidates=1 logout_attempts=0")


def test_deleting_volume_remains_protected_while_it_is_in_the_inventory() -> None:
    deleting = longhorn_session(113, "pvc-deleting")
    iscsi = FakeIscsi([(deleting,)])

    report = Reconciler(
        FakeInventory([{"pvc-deleting"}]), iscsi, FakeUsage(), apply=True
    ).run()

    assert iscsi.inspections == []
    assert iscsi.logouts == []
    assert report.lines[0].startswith("PROTECTED session=113")


def test_in_use_device_is_reported_for_manual_handling() -> None:
    orphan = longhorn_session(14, "pvc-orphan")
    iscsi = FakeIscsi([(orphan,)], {14: ("sdc",)})
    usage = FakeUsage({14: Usage(True, ("/dev/sdc mounted at /data",))})

    report = Reconciler(FakeInventory([{"pvc-live"}]), iscsi, usage, apply=True).run()

    assert iscsi.logouts == []
    assert report.failures == [
        "session=14 volume=pvc-orphan is in use: /dev/sdc mounted at /data"
    ]


def test_apply_logs_out_only_the_revalidated_orphan_session() -> None:
    live = longhorn_session(112, "pvc-live")
    orphan = longhorn_session(14, "pvc-orphan", "10.42.0.230:3260,1")
    iscsi = FakeIscsi(
        [
            (live, orphan),
            (live, orphan),
            (live, orphan),
            (live,),
        ]
    )
    inventory = FakeInventory([{"pvc-live"}, {"pvc-live"}, {"pvc-live"}])

    report = Reconciler(inventory, iscsi, FakeUsage(), apply=True).run()

    assert iscsi.logouts == [14]
    assert report.logout_attempts == [14]
    assert report.logout_successes == [14]
    assert report.failures == []
    assert any(line.startswith("LOGGED_OUT session=14") for line in report.lines)


def test_iscsi_logout_command_is_scoped_to_one_session_id() -> None:
    runner = QueueRunner([CommandResult(0)])
    session = longhorn_session(14, "pvc-orphan")

    IscsiClient(runner).logout(session)

    assert runner.commands == [("iscsiadm", "-m", "session", "-r", "14", "--logout")]


def test_volume_that_appears_before_logout_is_protected() -> None:
    orphan = longhorn_session(14, "pvc-orphan")
    iscsi = FakeIscsi([(orphan,)])
    inventory = FakeInventory([{"pvc-live"}, {"pvc-live", "pvc-orphan"}])

    report = Reconciler(inventory, iscsi, FakeUsage(), apply=True).run()

    assert iscsi.logouts == []
    assert any("appeared in the current inventory" in line for line in report.lines)


def test_changed_session_identity_is_not_logged_out() -> None:
    orphan = longhorn_session(14, "pvc-orphan")
    replacement = longhorn_session(14, "pvc-replacement", "10.42.0.91:3260,1")
    iscsi = FakeIscsi([(orphan,), (replacement,)])
    inventory = FakeInventory([{"pvc-live"}, {"pvc-live"}])

    report = Reconciler(inventory, iscsi, FakeUsage(), apply=True).run()

    assert iscsi.logouts == []
    assert report.failures == [
        "session=14 volume=pvc-orphan: session identity changed before logout"
    ]


@pytest.mark.parametrize(
    "logout_result, expected",
    [
        (CommandTimedOut("command timed out after 15s: iscsiadm"), "timed out"),
        (CommandResult(32, stderr="target likely not connected"), "exit 32"),
    ],
)
def test_timeout_and_logout_failure_are_not_retried(
    logout_result: CommandResult | Exception, expected: str
) -> None:
    orphan = longhorn_session(14, "pvc-orphan")
    iscsi = FakeIscsi([(orphan,), (orphan,), (orphan,)], logout_result=logout_result)
    inventory = FakeInventory([{"pvc-live"}, {"pvc-live"}, {"pvc-live"}])

    report = Reconciler(inventory, iscsi, FakeUsage(), apply=True).run()

    assert iscsi.logouts == [14]
    assert report.logout_attempts == [14]
    assert report.logout_successes == []
    assert len(report.failures) == 1
    assert expected in report.failures[0]


def test_session_still_present_after_successful_logout_is_reported_as_wedged() -> None:
    orphan = longhorn_session(14, "pvc-orphan")
    iscsi = FakeIscsi([(orphan,), (orphan,), (orphan,), (orphan,)])
    inventory = FakeInventory([{"pvc-live"}, {"pvc-live"}, {"pvc-live"}])

    report = Reconciler(inventory, iscsi, FakeUsage(), apply=True).run()

    assert iscsi.logouts == [14]
    assert report.logout_successes == []
    assert report.failures == [
        "session=14 volume=pvc-orphan: targeted logout returned success but the "
        "session remains"
    ]


def test_audit_is_streamed_before_an_unexpected_post_logout_failure() -> None:
    class UnexpectedPostLogoutIscsi(FakeIscsi):
        def sessions(self) -> tuple[Session, ...]:
            if self.logouts:
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
            return super().sessions()

    orphan = longhorn_session(14, "pvc-orphan")
    iscsi = UnexpectedPostLogoutIscsi([(orphan,), (orphan,), (orphan,)])
    inventory = FakeInventory([{"pvc-live"}, {"pvc-live"}, {"pvc-live"}])
    streamed: list[str] = []

    with pytest.raises(UnicodeDecodeError):
        Reconciler(inventory, iscsi, FakeUsage(), apply=True).run(
            ReconcileReport(sink=streamed.append)
        )

    assert iscsi.logouts == [14]
    assert any(line.startswith("CANDIDATE session=14") for line in streamed)
    assert any(line.startswith("LOGOUT_ATTEMPT session=14") for line in streamed)
    assert any(
        line.startswith("LOGOUT_RETURNED_SUCCESS session=14") for line in streamed
    )
