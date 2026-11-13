"""Producer payloads over bounded Kubernetes HTTP, evaluation and notifications."""

import asyncio
import json
import ssl
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from factory import advisory_signals as signals
from factory import health_alerts, ops_health

NOW = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
STAGES = [
    {"metadata": {"namespace": "kargo-embervm", "name": s}} for s in ("dev", "prod")
]


def promotion(phase, stage="dev", age=10):
    return {
        "apiVersion": "kargo.akuity.io/v1alpha1",
        "metadata": {
            "namespace": "kargo-embervm",
            "name": "p",
            "creationTimestamp": (NOW - timedelta(seconds=age)).isoformat(),
        },
        "spec": {"stage": stage, "freight": "f"},
        "status": {"phase": phase},
    }


def log(age, kind="control", level="info", message="healthy"):
    time = (NOW - timedelta(seconds=age)).isoformat()
    if kind == "egress":
        level = {"info": "INFO", "error": "ERROR"}.get(level, level)
    return (
        time
        + " "
        + json.dumps(
            {
                "time": time,
                "level": level,
                "message" if kind == "control" else "msg": message,
            }
        )
        + "\n"
    ).encode()


def pod(name="a", restarted=0, age=2000, container="control-plane"):
    return {
        "metadata": {"name": name},
        "status": {
            "containerStatuses": [
                {
                    "name": container,
                    "restartCount": restarted,
                    "state": {
                        "running": {
                            "startedAt": (NOW - timedelta(seconds=age)).isoformat()
                        }
                    },
                }
            ]
        },
    }


def run(coro):
    return asyncio.run(coro)


def test_promotion_recovery_is_terminal_and_per_stage():
    rows = [promotion("Failed", age=200), promotion("Succeeded", "prod", 100)]
    assert not signals.evaluate_promotions(STAGES, rows, NOW)["ok"]
    for phase in ("Pending", "Running", "Aborted"):
        assert not signals.evaluate_promotions(STAGES, rows + [promotion(phase)], NOW)[
            "ok"
        ]
    assert signals.evaluate_promotions(STAGES, rows + [promotion("Succeeded")], NOW)[
        "ok"
    ]
    assert not signals.evaluate_promotions(STAGES, rows + [promotion("Errored")], NOW)[
        "ok"
    ]
    # Old failures do not latch after a newer success in that same stage.
    assert signals.evaluate_promotions(
        STAGES[:1], [promotion("Failed", age=1000000), promotion("Succeeded")], NOW
    )["ok"]


@pytest.mark.parametrize(
    "change", ["same_time", "future", "naive", "malformed", "partial", "no_history"]
)
def test_ambiguous_promotion_cannot_clear(change):
    rows = [promotion("Succeeded"), promotion("Succeeded", "prod")]
    partial = False
    if change == "same_time":
        rows.append(promotion("Failed"))
    if change == "future":
        rows.append(promotion("Succeeded", age=-1))
    if change == "naive":
        rows[0]["metadata"]["creationTimestamp"] = "2026-11-13T11:59:00"
    if change == "malformed":
        rows.append({})
    if change == "partial":
        partial = True
    if change == "no_history":
        rows = []
    result = signals.evaluate_promotions(STAGES, rows, NOW, partial)
    assert not result["ok"] or result["status"] == "unknown"


def test_failures_in_other_pipelines_and_timestamp_order():
    pipelines = [
        "monolith",
        "embervm",
        "monolith-agents",
        "monolith-public",
        "scotland-connector",
    ]
    stages, rows = [], []
    for name in pipelines:
        stages.append({"metadata": {"namespace": "kargo-" + name, "name": "prod"}})
        row = promotion("Errored", "prod")
        row["metadata"]["namespace"] = "kargo-" + name
        rows.append(row)
    assert (
        "5 stage(s) with unresolved"
        in signals.evaluate_promotions(stages, rows, NOW)["detail"]
    )
    # List order and finish time are not attempt ordering.
    old = promotion("Failed", age=100)
    old["status"]["finishedAt"] = NOW.isoformat()
    assert signals.evaluate_promotions(STAGES[:1], [promotion("Succeeded"), old], NOW)[
        "ok"
    ]


@pytest.mark.parametrize("kind,threshold", [("control", 3), ("egress", 1)])
def test_log_window_threshold_boundary_and_recovery(kind, threshold):
    raw = log(930, kind) + log(900, kind, "error", "egress denied")
    assert signals.evaluate_log(raw, False, kind, NOW) == (0, False)
    raw += b"".join(log(899, kind, "error", "egress denied") for _ in range(threshold))
    assert signals.evaluate_log(raw, False, kind, NOW) == (threshold, False)
    assert signals.evaluate_log(raw, False, kind, NOW + timedelta(seconds=901))[0] == 0


@pytest.mark.parametrize("suffix", [b"broken\n", b"{", b"\xff\n"])
def test_malformed_or_truncated_logs_preserve_positive_evidence(suffix):
    raw = log(930) + log(1, level="error") + suffix
    assert signals.evaluate_log(raw, False, "control", NOW) == (1, True)


def test_log_uncertainty_and_specific_denial_messages():
    assert signals.evaluate_log(b"", False, "control", NOW) == (0, True)
    assert signals.evaluate_log(log(1), False, "control", NOW) == (0, True)
    assert signals.evaluate_log(log(-1), False, "control", NOW)[1]
    assert signals.evaluate_log(log(930) + log(1), True, "control", NOW)[1]
    raw = log(930, "egress")
    for message in (
        "egress denied",
        "egress denied: credential unresolved",
        "egress denied: plaintextUpstream entry resolved to a public address",
    ):
        raw += log(1, "egress", "WARN", message)
    raw += log(1, "egress", "ERROR", "egress upstream dial failed")
    raw += log(1, "egress", "INFO", "request status=403")
    assert signals.evaluate_log(raw, False, "egress", NOW) == (3, False)
    assert signals.evaluate_log(
        log(930) + log(1, level="warning", message="session create failed"),
        False,
        "control",
        NOW,
    ) == (0, False)


@pytest.mark.parametrize(
    "message",
    [
        "egress denied",
        "egress denied: credential unresolved",
        "egress denied: credential unresolved for host",
        "egress denied: plaintextUpstream entry resolved to a public address; refusing to send a credential in cleartext",
        "egress swap: authority mismatch; request denied",
        "egress swap: claim injection failed; request denied",
        "egress swap: credential trailer denied",
    ],
)
def test_egress_producer_refusals_each_cross_threshold(message):
    # Literal messages from egress-proxy/cmd/main.go and cmd/swap.go.
    def handler(request):
        if request.url.path.endswith("/pods"):
            return httpx.Response(200, json={"items": [pod(container="egress-proxy")]})
        return httpx.Response(
            200, content=log(930, "egress") + log(1, "egress", "WARN", message)
        )

    result = run(transport_collect(handler, kind="egress"))
    assert not result["ok"] and "1 egress denial" in result["detail"]


@pytest.mark.parametrize("kind", ["control", "egress"])
def test_records_emitted_during_read_do_not_count_or_make_window_unknown(kind):
    raw = log(930, kind) + log(1, kind)
    raw += log(-0.2, kind, "error", "egress denied")
    assert signals.evaluate_log(raw, False, kind, NOW) == (0, False)
    # The same record belongs to the next observation's window.
    assert signals.evaluate_log(raw, False, kind, NOW + timedelta(seconds=1)) == (
        1,
        False,
    )


def test_kubelet_nanosecond_timestamp():
    raw = log(930, "egress") + log(1, "egress", "WARN", "egress denied").replace(
        b"2026-11-13T11:59:59+00:00 ", b"2026-11-13T11:59:59.123456789Z ", 1
    )
    assert signals.evaluate_log(raw, False, "egress", NOW) == (1, False)


async def transport_collect(handler, kind="control", targets=None):
    async with httpx.AsyncClient(
        base_url="https://kubernetes", transport=httpx.MockTransport(handler)
    ) as http:
        return await signals.collect_logs(
            signals.Reader(http),
            targets
            or [{"namespace": "embervm", "selector": "app.kubernetes.io/name=embervm"}],
            kind,
            NOW,
        )


def test_transport_multiple_pods_and_bounded_parameters():
    reads = []

    def handler(request):
        reads.append(request)
        if request.url.path.endswith("/pods"):
            assert request.url.params["limit"] == str(signals.MAX_PODS)
            return httpx.Response(
                200, json={"items": [pod("a"), pod("b")], "metadata": {}}
            )
        assert request.url.params["container"] == "control-plane"
        assert request.url.params["sinceSeconds"] == "960"
        assert request.url.params["tailLines"] == "2000"
        assert request.url.params["limitBytes"] == "262144"
        return httpx.Response(200, content=log(930) + log(1, level="error") * 2)

    result = run(transport_collect(handler))
    assert not result["ok"] and "4 control-plane error" in result["detail"]
    assert len(reads) == 3


@pytest.mark.parametrize(
    "fault",
    [
        "403",
        "timeout",
        "malformed",
        "pagination",
        "young",
        "absent",
        "byte_cap",
        "line_cap",
    ],
)
def test_transport_uncertainty_cannot_prove_recovery(fault):
    def handler(request):
        if request.url.path.endswith("/pods"):
            if fault == "403":
                return httpx.Response(403, text="SECRET exception body")
            if fault == "timeout":
                raise httpx.ReadTimeout("SECRET")
            if fault == "malformed":
                return httpx.Response(200, text="broken")
            pods = (
                [] if fault == "absent" else [pod(age=10 if fault == "young" else 2000)]
            )
            return httpx.Response(
                200,
                json={
                    "items": pods,
                    "metadata": {"continue": "more" if fault == "pagination" else ""},
                },
            )
        raw = log(930) + log(1)
        if fault == "byte_cap":
            raw += b"x" * signals.LOG_BYTES
        if fault == "line_cap":
            raw = log(930) + log(1) * signals.LOG_LINES
        return httpx.Response(200, content=raw)

    result = run(transport_collect(handler))
    assert result["status"] == "unknown"
    assert "SECRET" not in str(result)


@pytest.mark.parametrize("restarts", [0, 1])
@pytest.mark.parametrize("age,known", [(2000, True), (960, True), (959, False)])
def test_current_container_window_determines_recovery_after_restart(
    restarts, age, known
):
    def handler(request):
        if request.url.path.endswith("/pods"):
            return httpx.Response(
                200, json={"items": [pod(restarted=restarts, age=age)]}
            )
        return httpx.Response(200, content=log(930) + log(1))

    result = run(transport_collect(handler))
    assert result["ok"]
    assert (result.get("status") != "unknown") is known


def test_known_failure_survives_forbidden_pod():
    def handler(request):
        if request.url.path.endswith("/pods"):
            return httpx.Response(200, json={"items": [pod("a"), pod("b")]})
        if "/b/" in request.url.path:
            return httpx.Response(403)
        return httpx.Response(200, content=log(930) + log(1, level="error") * 3)

    result = run(transport_collect(handler))
    assert not result["ok"] and "incomplete coverage" in result["detail"]


def test_kargo_transport_complete_and_paginated():
    async def collect(partial):
        def handler(request):
            is_stage = request.url.path.endswith("/stages")
            assert request.url.params["limit"] == str(
                signals.MAX_STAGES if is_stage else signals.MAX_PROMOTIONS
            )
            return httpx.Response(
                200,
                json={
                    "items": STAGES
                    if is_stage
                    else [promotion("Succeeded"), promotion("Succeeded", "prod")],
                    "metadata": {"continue": "more" if partial else ""},
                },
            )

        async with httpx.AsyncClient(
            base_url="https://kubernetes", transport=httpx.MockTransport(handler)
        ) as http:
            return await signals.collect_promotions(signals.Reader(http), NOW)

    assert run(collect(False))["ok"]
    assert run(collect(True))["status"] == "unknown"


def test_default_off_no_reads_even_when_cache_contains_failure(monkeypatch):
    def forbidden():
        raise AssertionError("disabled probe performed a source read")

    monkeypatch.setattr(signals, "client", forbidden)
    for name, env in [
        ("kargo_promotions", "KARGO_PROMOTIONS"),
        ("embervm_control_errors", "EMBERVM_CONTROL_ERRORS"),
        ("egress_denials", "EGRESS_DENIALS"),
    ]:
        monkeypatch.delenv(env + "_ENABLED", raising=False)
        assert (
            run(
                signals.observe(
                    {
                        "kargo_promotions": "kargo",
                        "embervm_control_errors": "control",
                        "egress_denials": "egress",
                    }[name]
                )
            )["status"]
            == "unknown"
        )
        check = ops_health.CHECKS[name]
        check.reset()
        check._value = (float("inf"), {"ok": False})
        assert run(check())["status"] == "unknown"
        check.reset()


def test_cache_single_flight_and_timeout():
    async def exercise():
        calls = []

        async def compute():
            calls.append(1)
            await asyncio.sleep(0.01)
            return {"ok": False, "detail": "aggregate only"}

        check = ops_health._CachedCheck("test", compute)
        values = await asyncio.gather(*(check() for _ in range(8)))
        assert len(calls) == 1 and all(not v["ok"] for v in values)
        timed = ops_health._CachedCheck("test", compute, timeout_s=0.001)
        assert (await timed())["status"] == "unknown"

    run(exercise())


def test_existing_notification_path_unknown_dedup_and_recovery(monkeypatch):
    import shared.notify

    previous, messages = {}, []
    monkeypatch.setattr(health_alerts, "_read_sync", lambda names: dict(previous))

    def write(name, ok, detail, now):
        previous[name] = (ok, now)

    monkeypatch.setattr(health_alerts, "_write_sync", write)

    async def notify(message, **kwargs):
        messages.append(message)

    monkeypatch.setattr(shared.notify, "notify", notify)
    state = [signals.evaluate_promotions(STAGES[:1], [promotion("Failed")], NOW)]

    async def check():
        return state[0]

    checks = {"kargo_promotions": check}
    run(health_alerts.run_cycle(checks, now=NOW))
    run(health_alerts.run_cycle(checks, now=NOW))
    state[0] = signals.unknown("disabled")
    run(health_alerts.run_cycle(checks, now=NOW))
    assert len(messages) == 1 and not previous["kargo_promotions"][0]
    state[0] = signals.evaluate_promotions(STAGES[:1], [promotion("Succeeded")], NOW)
    run(health_alerts.run_cycle(checks, now=NOW))
    assert len(messages) == 2 and "recovered" in messages[-1]


@pytest.mark.parametrize("rotate", [False, True])
def test_incluster_client_awaits_rotating_token(monkeypatch, tmp_path, rotate):
    from kubernetes_asyncio import client as kube, config
    from kubernetes_asyncio.config.incluster_config import InClusterConfigLoader

    token = tmp_path / "token"
    token.write_text("fixture-only")
    ca = tmp_path / "ca.crt"
    certificate = httpx.create_ssl_context(trust_env=False).get_ca_certs(
        binary_form=True
    )[0]
    ca.write_text(ssl.DER_cert_to_PEM_cert(certificate))
    loader = InClusterConfigLoader(
        token_filename=str(token),
        cert_filename=str(ca),
        environ={
            "KUBERNETES_SERVICE_HOST": "kubernetes.invalid",
            "KUBERNETES_SERVICE_PORT": "443",
        },
    )

    def load():
        loader.load_and_set()
        if rotate:
            token.write_text("rotated-fixture")
            loader.token_expires_at = datetime.min

    monkeypatch.setattr(kube.Configuration, "_default", None)
    monkeypatch.setattr(config, "load_incluster_config", load)

    async def exercise():
        async with await signals.client() as http:
            request = http.build_request("GET", "/api/v1/pods")
            expected = "rotated-fixture" if rotate else "fixture-only"
            assert request.headers["Authorization"] == "Bearer " + expected
            assert str(http.base_url) == "https://kubernetes.invalid"

    run(exercise())


def test_enabled_runtime_payloads_and_redacted_forbidden(monkeypatch):
    monkeypatch.setenv("KARGO_PROMOTIONS_ENABLED", "true")

    async def client():
        return httpx.AsyncClient(
            base_url="https://kubernetes",
            transport=httpx.MockTransport(
                lambda r: httpx.Response(403, text="secret body")
            ),
        )

    monkeypatch.setattr(signals, "client", client)
    assert run(signals.observe("kargo")) == signals.unknown(
        "source unreadable (HTTPStatusError)"
    )


def test_request_wall_deadline_and_stream_byte_cap():
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(2)
            yield b"late"

    async def exercise():
        async with httpx.AsyncClient(
            base_url="https://kubernetes",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Slow())),
        ) as http:
            with pytest.raises(TimeoutError):
                await signals.Reader(http).read("/log", {}, 32)
        async with httpx.AsyncClient(
            base_url="https://kubernetes",
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=b"x" * 100000)
            ),
        ) as http:
            data, capped = await signals.Reader(http).read("/log", {}, 32)
            assert len(data) == 32 and capped

    run(exercise())


def test_pod_budget_and_concurrency():
    async def exercise():
        active = peak = reads = 0

        async def handler(request):
            nonlocal active, peak, reads
            if request.url.path.endswith("/pods"):
                return httpx.Response(
                    200, json={"items": [pod(str(i)) for i in range(12)]}
                )
            reads += 1
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return httpx.Response(200, content=log(930) + log(1))

        targets = [{"namespace": "embervm" + str(i), "selector": "x"} for i in range(4)]
        result = await transport_collect(handler, targets=targets)
        assert result["status"] == "unknown"  # omitted pods cannot clear
        assert reads == signals.MAX_PODS and peak == 3

    run(exercise())


@pytest.mark.parametrize("errors,healthy", [(2, True), (3, False)])
def test_control_threshold_from_transport(errors, healthy):
    def handler(request):
        if request.url.path.endswith("/pods"):
            return httpx.Response(200, json={"items": [pod()]})
        return httpx.Response(200, content=log(930) + log(1, level="error") * errors)

    assert run(transport_collect(handler))["ok"] is healthy


def test_malformed_stage_payload_does_not_mask_observed_failure():
    rows = [promotion("Failed"), promotion("Succeeded", "prod")]
    rows[1]["status"] = []
    result = signals.evaluate_promotions(STAGES, rows, NOW)
    assert not result["ok"] and "incomplete coverage" in result["detail"]


def test_stale_clock_skew_and_unrecognized_level_are_unknown():
    assert signals.evaluate_log(log(2000), False, "control", NOW)[1]
    raw = log(930) + log(1).replace(b'"level": "info"', b'"level": "garbled"')
    assert signals.evaluate_log(raw, False, "control", NOW)[1]
    raw = log(930) + log(1).replace(
        b'"time": "2026-11-13T11:59:59+00:00"', b'"time": "2026-11-12T11:59:59+00:00"'
    )
    assert signals.evaluate_log(raw, False, "control", NOW)[1]
