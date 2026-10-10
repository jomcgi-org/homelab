"""Real DBOS processes: handoff cessation, persisted sleeps and native fences.

SQLite and file-backed native-result seams are isolated from production. DBOS
itself, its queue, checkpoints, recovery and execute_node are never mocked.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _wait(predicate, message, timeout=25):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        time.sleep(0.02)


class Processes:
    def __init__(self, directory, mode):
        self.directory = directory
        self.mode = mode
        self.children = []
        self.logs = []

    def launch(self, role):
        log = (self.directory / f"{role}.log").open("w")
        self.logs.append(log)
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                self.mode,
                role,
                str(self.directory),
            ],
            stdout=log,
            stderr=log,
        )
        self.children.append(child)
        return child

    def rows(self, sql):
        try:
            with sqlite3.connect(self.directory / "dbos.sqlite") as connection:
                return connection.execute(sql).fetchall()
        except sqlite3.OperationalError:
            return []

    def state(self):
        return self.rows(
            "SELECT status,recovery_attempts FROM workflow_status WHERE workflow_uuid='original-node'"
        )

    def close(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        for log in self.logs:
            log.close()


def test_lifespan_ceases_executor_before_recovery(tmp_path):
    """Fails on the old cancel-before-drain ordering with the SDK's real waiter."""
    processes = Processes(tmp_path, "handoff")
    try:
        a = processes.launch("A")
        _wait(lambda: (tmp_path / "poll-A").exists(), "A never entered poll")
        (tmp_path / "stop").touch()
        _wait(lambda: (tmp_path / "draining").exists(), "lifespan never drained")
        _wait(
            lambda: (
                (tmp_path / "released").exists()
                or (tmp_path / "renewed-during-drain").exists()
            ),
            "lease neither renewed nor released",
        )
        b = None
        if (tmp_path / "released").exists():
            b = processes.launch("B")
            _wait(lambda: (tmp_path / "poll-B").exists(), "B did not recover")
        (tmp_path / "go-A").touch()
        _wait(
            lambda: (
                len(
                    processes.rows(
                        "SELECT function_id FROM operation_outputs WHERE workflow_uuid='original-node'"
                    )
                )
                >= 3
            ),
            "A did not checkpoint its polling sleep",
        )
        if b is not None:
            (tmp_path / "go-B").touch()
            _wait(
                lambda: (
                    "Aborting duplicate execution" in (tmp_path / "B.log").read_text()
                ),
                "overlap did not reach the duplicate-result waiter",
            )
        (tmp_path / "finish-drain").touch()
        if b is None:
            _wait(lambda: a.poll() is not None, "old DBOS process did not cease")
            assert a.returncode == 0
            assert (tmp_path / "module-shutdown").exists()
            assert not (tmp_path / "released").exists()
        else:
            # Reproduce disappearance of the executor which won the checkpoint.
            a.kill()
            a.wait(timeout=5)
        (tmp_path / "native-result").touch()
        if b is None:
            (tmp_path / "go-B").touch()
            processes.launch("B")
        deadline = time.monotonic() + 8
        while processes.state() != [("SUCCESS", 2)] and time.monotonic() < deadline:
            time.sleep(0.02)
        assert processes.state() == [("SUCCESS", 2)], (
            "recovered executor stranded in duplicate-result waiter"
        )
    finally:
        processes.close()


@pytest.mark.parametrize("result_kind", ["completed", "live", "unknown", "paused"])
def test_node_recovery_preserves_invocation_and_fences(tmp_path, result_kind):
    processes = Processes(tmp_path, "node")
    (tmp_path / "plan.json").write_text(json.dumps({"ok": True}))
    (tmp_path / "kind").write_text(result_kind)
    try:
        a = processes.launch("A")
        _wait(
            lambda: processes.rows(
                "SELECT function_id FROM operation_outputs WHERE workflow_uuid='original-node' AND function_name='DBOS.sleep'"
            ),
            "execute_node never persisted its polling sleep",
        )
        assert (tmp_path / "invocations").read_text().splitlines() == [
            "t-recovery:implement:1:101"
        ]
        a.kill()
        a.wait(timeout=5)
        if result_kind == "paused":
            (tmp_path / "operator-paused").touch()
        processes.launch("B")
        _wait(
            lambda: (tmp_path / "poll-B").exists(),
            "recovered node never resumed polling",
        )
        assert processes.state() == [("PENDING", 2)]
        if result_kind in {"completed", "unknown"}:
            (tmp_path / "native-result").touch()
            _wait(
                lambda: (tmp_path / "settlement.json").exists(),
                "late native result did not settle the original attempt",
            )
            result = json.loads((tmp_path / "settlement.json").read_text())
            assert result["session_id"] == 101
            assert result["attempt"] == 1
            if result_kind == "completed":
                assert result["status"] == "succeeded"
                assert result["value"] == {"ok": True}
                assert (tmp_path / "artifact-reads").read_text() == "101:1:plan.json"
            else:
                assert result["status"] == "uncertain"
                assert "unknown_invocation" in result["reason"]
                assert not (tmp_path / "artifact-reads").exists()
        else:
            # Observe a second live poll after recovery, rather than a snapshot
            # taken before the recovered executor actually started.
            _wait(
                lambda: (tmp_path / "polls-B").read_text().count("poll\n") >= 2,
                "live recovered invocation did not keep waiting",
            )
            assert processes.state() == [("PENDING", 2)]
            assert not (tmp_path / "settlement.json").exists()
            assert not (tmp_path / "artifact-reads").exists()
            if result_kind == "paused":
                assert (tmp_path / "operator-paused").exists()
        assert (tmp_path / "invocations").read_text().splitlines() == [
            "t-recovery:implement:1:101"
        ]
    finally:
        processes.close()


def _worker(mode, role, directory):
    from dbos import DBOS, Queue, SetWorkflowID

    from factory.orchestration import runtime

    p = Path(directory)
    if mode == "handoff":

        @DBOS.step()
        def poll():
            (p / f"poll-{role}").touch()
            _wait(lambda: (p / f"go-{role}").exists(), "poll barrier")
            return (p / "native-result").exists()

        @DBOS.workflow()
        def node():
            DBOS.sleep(0.1)
            while not poll():
                DBOS.sleep(1)
            return "original-session"

        DBOS(
            config={
                "name": "handoff",
                "system_database_url": f"sqlite:///{p / 'dbos.sqlite'}",
                "executor_id": "local",
                "application_version": "v1",
                "enable_patching": True,
            }
        )
        queue = Queue("nodes", worker_concurrency=1)
        if role == "A":
            from core import leadership
            from fastapi import FastAPI
            from framework import PRIVATE_PROFILE, Module, build_private_lifespan

            def renew(*_args):
                if (p / "draining").exists():
                    (p / "renewed-during-drain").touch()
                return True

            leadership._acquire_or_renew = renew
            leadership._release = lambda *_args: (p / "released").touch()
            leadership.RENEW_INTERVAL = 0.05

            async def start(app):
                app.state.leader_shutdown_exit = runtime.exit_process
                DBOS.launch()
                with SetWorkflowID("original-node"):
                    queue.enqueue(node)
                return []

            async def stop(_app):
                (p / "draining").touch()
                while not (p / "finish-drain").exists():
                    await asyncio.sleep(0.01)

            async def shutdown(_app):
                (p / "module-shutdown").touch()

            async def lifespan():
                module = Module(
                    name="dbos", leader_start=start, leader_stop=stop, shutdown=shutdown
                )
                async with build_private_lifespan(PRIVATE_PROFILE, [module])(FastAPI()):
                    while not (p / "stop").exists():
                        await asyncio.sleep(0.01)

            asyncio.run(lifespan())
        else:
            DBOS.launch()
    else:
        from factory.orchestration import node_workflows as nodes

        # Compute the deployed durable version before substituting native I/O
        # seams. Both child processes run the unchanged execute_node source.
        config = runtime.build_dbos_config(f"sqlite:///{p / 'dbos.sqlite'}")

        @DBOS.step()
        def start(pin, _key, _prompt, _deadline):
            assert not (p / "operator-paused").exists(), "paused task dispatched again"
            with (p / "invocations").open("a") as output:
                output.write(
                    f"{pin['task_id']}:{pin['node_key']}:{pin['attempt']}:101\n"
                )
            return {"started": True, "session_id": 101}

        @DBOS.step()
        def poll_turn(session_id, _after_seq):
            assert session_id == 101
            (p / f"poll-{role}").touch()
            with (p / f"polls-{role}").open("a") as output:
                output.write("poll\n")
            if not (p / "native-result").exists():
                return None
            return {
                "seq": 1,
                "terminal_reason": "completed",
                "cost_usd": 0.5,
                "stop_reason": "invocation_outcome_unknown"
                if (p / "kind").read_text() == "unknown"
                else "end_turn",
            }

        @DBOS.step()
        def dispatch(_pin, session_id):
            assert session_id == 101
            return {"state": "dispatched", "started_at": (p / "started-at").read_text()}

        @DBOS.step()
        def clock():
            return datetime.now(timezone.utc).isoformat()

        @DBOS.step()
        def artifact(session_id, seq, path, _schema):
            assert (session_id, seq, path) == (101, 1, "plan.json")
            (p / "artifact-reads").write_text(f"{session_id}:{seq}:{path}")
            return {
                "status": "ok",
                "value": json.loads((p / path).read_text()),
                "errors": [],
            }

        @DBOS.step()
        def head(_repo, _branch):
            return "a" * 40

        @DBOS.step()
        def cleanup(_workflow_id):
            return {"status": "pending"}

        nodes._start_node_session = start
        nodes.poll_turn = poll_turn
        nodes._read_node_dispatch = dispatch
        nodes.observe_clock = clock
        nodes._read_turn_artifact = artifact
        nodes.read_branch_head = head
        nodes._cleanup_node = cleanup
        DBOS(config=config)
        DBOS.launch()
        if role == "A":
            now = datetime.now(timezone.utc)
            (p / "started-at").write_text(now.isoformat())
            pin = {
                "task_id": "t-recovery",
                "node_key": "implement",
                "attempt": 1,
                "repo": "org/repo",
                "branch": "factory/recovery",
                "prompt": "test",
                "model": "luna",
                "max_cost_usd": 2.0,
                "max_attempts": 3,
                "turn_timeout_seconds": 120,
                "workflow_id": "original-node",
                "artifact_path": "plan.json",
                "artifact_schema": {"type": "object"},
                "task_deadline_at": (now + timedelta(seconds=120)).isoformat(),
            }
            with SetWorkflowID("original-node"):
                DBOS.start_workflow(nodes.execute_node, pin)
        handle = DBOS.retrieve_workflow("original-node")
        while True:
            status = handle.get_status()
            if status is not None and status.status in {"SUCCESS", "ERROR"}:
                (p / "settlement.json").write_text(json.dumps(handle.get_result()))
                break
            time.sleep(0.02)
    while True:
        time.sleep(0.02)


if __name__ == "__main__":
    _worker(*sys.argv[1:])
