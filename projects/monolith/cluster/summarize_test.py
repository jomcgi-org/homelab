"""Unit tests for cluster.summarize — the token-efficiency layer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cluster import summarize


def _ts(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestAge:
    def test_days_and_hours(self):
        assert summarize._age(_ts(days=3, hours=4)).endswith("h")
        assert summarize._age(_ts(days=3, hours=4)).startswith("3d")

    def test_minutes_only(self):
        assert summarize._age(_ts(minutes=5)) == "5m"

    def test_none_and_bad_input(self):
        assert summarize._age(None) is None
        assert summarize._age("not-a-date") is None


class TestResourceRow:
    def test_pod_row_reports_ready_and_restarts(self):
        obj = {
            "metadata": {
                "name": "p1",
                "namespace": "ns",
                "creationTimestamp": _ts(minutes=1),
            },
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {"ready": True, "restartCount": 0, "state": {"running": {}}},
                    {
                        "ready": False,
                        "restartCount": 3,
                        "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                    },
                ],
            },
        }
        row = summarize.resource_row("pods", obj)
        assert row["name"] == "p1"
        assert row["ready"] == "1/2"
        assert row["restarts"] == 3
        assert row["reason"] == "CrashLoopBackOff"

    def test_deployment_ready_ratio(self):
        obj = {
            "metadata": {"name": "d1", "namespace": "ns"},
            "spec": {"replicas": 3},
            "status": {"readyReplicas": 2},
        }
        assert summarize.resource_row("deployments", obj)["ready"] == "2/3"

    def test_application_sync_health(self):
        obj = {
            "metadata": {"name": "a1"},
            "status": {
                "sync": {"status": "OutOfSync"},
                "health": {"status": "Degraded"},
            },
        }
        row = summarize.resource_row("applications", obj)
        assert row["sync"] == "OutOfSync"
        assert row["health"] == "Degraded"


class TestUnhealthy:
    def test_running_ready_pod_is_healthy(self):
        row = {"phase": "Running", "ready": "2/2"}
        assert not summarize._row_unhealthy("pods", row)

    def test_crashloop_pod_is_unhealthy(self):
        assert summarize._row_unhealthy(
            "pods", {"phase": "Running", "reason": "CrashLoopBackOff"}
        )

    def test_succeeded_pod_is_healthy(self):
        # A completed Job/CronWorkflow pod reports ready=0/1 (containers
        # terminated); it must not be flagged as unhealthy.
        assert not summarize._row_unhealthy(
            "pods", {"phase": "Succeeded", "ready": "0/1"}
        )

    def test_inflight_batch_pod_is_healthy(self):
        # An Argo Workflow / Job pod (restartPolicy Never) that is mid-run reports
        # ready=x/N (x != N) with a wait sidecar and never becomes Ready. It must
        # not be flagged just for being in flight, else a per-minute snapshot
        # constantly reports the cluster unhealthy.
        assert not summarize._row_unhealthy(
            "pods", {"phase": "Running", "ready": "0/2", "restart_policy": "Never"}
        )

    def test_pending_batch_pod_is_healthy(self):
        assert not summarize._row_unhealthy(
            "pods",
            {
                "phase": "Pending",
                "reason": "ContainerCreating",
                "restart_policy": "Never",
            },
        )

    def test_failed_batch_pod_is_unhealthy(self):
        # A terminal failure IS worth surfacing (failed Workflow pods are kept by
        # podGC OnWorkflowSuccess, so they persist rather than flicker).
        assert summarize._row_unhealthy(
            "pods", {"phase": "Failed", "restart_policy": "Never"}
        )

    def test_running_unready_long_lived_pod_still_unhealthy(self):
        # A Deployment pod (restartPolicy Always) stuck unready keeps the old
        # behaviour: it is meant to be Ready, so x/N with x != N is unhealthy.
        assert summarize._row_unhealthy(
            "pods", {"phase": "Running", "ready": "0/1", "restart_policy": "Always"}
        )

    def test_partial_deployment_is_unhealthy(self):
        assert summarize._row_unhealthy("deployments", {"ready": "1/3"})

    def test_synced_healthy_app_is_ok(self):
        assert not summarize._row_unhealthy(
            "applications", {"sync": "Synced", "health": "Healthy"}
        )


class TestBuildHealth:
    def test_only_unhealthy_returned(self):
        resources = {
            "deployments": [
                {
                    "metadata": {"name": "ok"},
                    "spec": {"replicas": 1},
                    "status": {"readyReplicas": 1},
                },
                {
                    "metadata": {"name": "bad"},
                    "spec": {"replicas": 2},
                    "status": {"readyReplicas": 0},
                },
            ]
        }
        out = summarize.build_health(resources)
        assert out["healthy"] is False
        assert out["scanned"] == 2
        names = [r["name"] for r in out["unhealthy"]["deployments"]]
        assert names == ["bad"]

    def test_all_healthy(self):
        resources = {
            "pods": [
                {
                    "metadata": {"name": "p"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [{"ready": True, "restartCount": 0}],
                    },
                }
            ]
        }
        out = summarize.build_health(resources)
        assert out["healthy"] is True
        assert out["unhealthy"] == {}

    def test_completed_batch_pod_is_healthy(self):
        # Real Succeeded-pod shape: containers terminated, so ready is False.
        # This is the path that previously miscounted every finished
        # Job/CronWorkflow pod as unhealthy.
        resources = {
            "pods": [
                {
                    "metadata": {"name": "cron-1783792800"},
                    "status": {
                        "phase": "Succeeded",
                        "containerStatuses": [{"ready": False, "restartCount": 0}],
                    },
                }
            ]
        }
        out = summarize.build_health(resources)
        assert out["healthy"] is True
        assert out["unhealthy"] == {}

    def test_inflight_workflow_pod_is_healthy_end_to_end(self):
        # Real in-flight Argo Workflow pod shape: restartPolicy Never, Running,
        # main container not yet ready alongside the wait sidecar (ready 0/2).
        # Exercises restart_policy extraction from spec through build_health.
        resources = {
            "pods": [
                {
                    "metadata": {"name": "knowledge-ingest-1783894500"},
                    "spec": {"restartPolicy": "Never"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [
                            {"ready": False, "restartCount": 0},
                            {"ready": False, "restartCount": 0},
                        ],
                    },
                }
            ]
        }
        out = summarize.build_health(resources)
        assert out["healthy"] is True
        assert out["unhealthy"] == {}


class TestDedupeEvents:
    def test_collapses_repeats_with_count(self):
        events = [
            {
                "involvedObject": {"kind": "Pod", "name": "p1", "namespace": "ns"},
                "type": "Warning",
                "reason": "BackOff",
                "message": "boom",
                "count": 2,
                "lastTimestamp": "2026-05-31T10:00:00Z",
            },
            {
                "involvedObject": {"kind": "Pod", "name": "p1", "namespace": "ns"},
                "type": "Warning",
                "reason": "BackOff",
                "message": "boom",
                "count": 3,
                "lastTimestamp": "2026-05-31T11:00:00Z",
            },
        ]
        out = summarize.dedupe_events(events)
        assert len(out) == 1
        assert out[0]["count"] == 5
        assert out[0]["last_seen"] == "2026-05-31T11:00:00Z"
        assert out[0]["object"] == "Pod/p1"

    def test_distinct_messages_not_merged(self):
        events = [
            {
                "involvedObject": {"kind": "Pod", "name": "p"},
                "reason": "A",
                "message": "x",
            },
            {
                "involvedObject": {"kind": "Pod", "name": "p"},
                "reason": "B",
                "message": "y",
            },
        ]
        assert len(summarize.dedupe_events(events)) == 2


class TestFilterLogs:
    def test_grep_filters_lines(self):
        text = "info: ok\nERROR: bad\ninfo: fine\nERROR: worse\n"
        out = summarize.filter_logs(text, grep="ERROR")
        assert out["filtered"] is True
        assert out["lines"] == 2
        assert "ERROR: bad" in out["logs"]
        assert "info: ok" not in out["logs"]

    def test_tail_truncates(self):
        text = "\n".join(f"line{i}" for i in range(500))
        out = summarize.filter_logs(text, max_lines=10)
        assert out["lines"] == 10
        assert out["truncated"] is True
        assert "line499" in out["logs"]

    def test_no_grep_no_truncate(self):
        out = summarize.filter_logs("a\nb\nc", max_lines=200)
        assert out.get("filtered") is None
        assert out.get("truncated") is None
        assert out["lines"] == 3
