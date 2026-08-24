"""Guard the SigNoz alert ConfigMaps and the public /health probe wiring (#4867).

The public composite /health (https://jomcgi.dev/health) was 503 for 30+ minutes
during #4865 with nothing watching it. The fix is three coupled pieces, and a
silent regression in any one of them recreates the blind spot:

1. The httpcheck receiver in values-prod.yaml probes the endpoint (without it
   there is no httpcheck.status series to alert on).
2. public-health-httpcheck-alert.yaml alerts when that series reports failure,
   sustained (5 consecutive evaluations over 10 minutes), not on a single blip.
3. The ConfigMap is registered in kustomization.yaml (an unregistered manifest
   never reaches the cluster, so the sidecar never syncs it).

The advisory property is structural: the frontend keeps /health at 200 (with a
degraded[] list) when only advisory components fail (cd chart lag, red CI), and
httpcheck.status is 1 for any 2xx, so the alert keys on real HTTP failure only.
The test pins the pieces of that chain this repo owns in config.

This reads the YAML files directly (not rendered output) to stay free of a Helm
toolchain, mirroring monolith's public_turnstile_secret_isolation_test.py.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
import yaml

PUBLIC_HEALTH_URL = "https://jomcgi.dev/health"
PUBLIC_HEALTH_ALERT = "public-health-httpcheck-alert.yaml"
ALERT_CONFIGMAPS = (
    "signoz-httpcheck-alert.yaml",
    "jomcgi-dev-httpcheck-alert.yaml",
    PUBLIC_HEALTH_ALERT,
)


def _signoz_dir() -> Path:
    srcdir = os.environ.get("TEST_SRCDIR", "")
    candidate = Path(srcdir) / "_main" / "projects" / "platform" / "signoz"
    if candidate.exists():
        return candidate
    # Fallback for a direct (non-bazel) run from the repo root.
    here = Path(__file__).resolve().parent.parent.parent / "platform" / "signoz"
    if here.exists():
        return here
    raise FileNotFoundError(
        f"signoz dir not found at {candidate} or {here} (TEST_SRCDIR={srcdir!r})"
    )


def _load_yaml(name: str) -> dict:
    return yaml.safe_load((_signoz_dir() / name).read_text())


def _alert_json(name: str) -> tuple[dict, dict]:
    """Return (ConfigMap object, parsed alert.json) for an alert ConfigMap."""
    doc = _load_yaml(name)
    return doc, json.loads(doc["data"]["alert.json"])


def _alert_docs() -> dict[str, tuple[dict, dict]]:
    return {name: _alert_json(name) for name in ALERT_CONFIGMAPS}


def _parse_duration(value: str) -> float:
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", value)
    assert match is not None, f"unparseable duration {value!r}"
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


# ---------------------------------------------------------------------------
# Every alert ConfigMap is discoverable and routable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ALERT_CONFIGMAPS)
def test_alert_configmap_is_labeled_for_the_sidecar(name):
    doc, _ = _alert_json(name)
    assert doc["metadata"]["labels"]["signoz.io/alert"] == "true", (
        f"{name} lost its sidecar discovery label; the rule silently vanishes "
        "from SigNoz on the next prune"
    )


@pytest.mark.parametrize("name", ALERT_CONFIGMAPS)
def test_alert_routes_to_incidentio_consistently(name):
    """Annotation, preferredChannels, and thresholds must name the same channel."""
    doc, alert = _alert_json(name)
    annotated = doc["metadata"]["annotations"]["signoz.io/notification-channels"]
    thresholds = alert["condition"]["thresholds"]["spec"]
    channels_per_threshold = [sorted(t["channels"]) for t in thresholds]
    assert sorted(annotated.split(",")) == sorted(alert["preferredChannels"]), (
        f"{name} annotation channel {annotated!r} != preferredChannels "
        f"{alert['preferredChannels']}"
    )
    assert all(
        chans == sorted(alert["preferredChannels"]) for chans in channels_per_threshold
    ), f"{name} threshold channels {channels_per_threshold} != preferredChannels"


@pytest.mark.parametrize("name", ALERT_CONFIGMAPS)
def test_alert_json_carries_both_threshold_forms(name):
    """SigNoz v0.113 panics without legacy condition fields (see observability
    docs): op/target/matchType must sit on the condition AND in thresholds."""
    _, alert = _alert_json(name)
    condition = alert["condition"]
    spec = condition["thresholds"]["spec"][0]
    for field in ("op", "target", "matchType"):
        assert condition[field] == spec[field], f"{name}: condition/{field} mismatch"
        assert condition[field] != "", f"{name}: empty legacy {field}"


# ---------------------------------------------------------------------------
# The public /health alert does its one job
# ---------------------------------------------------------------------------


def test_public_health_alert_targets_the_health_endpoint():
    _, alert = _alert_json(PUBLIC_HEALTH_ALERT)
    query = alert["condition"]["compositeQuery"]["queries"][0]["spec"]
    expression = query["filter"]["expression"]
    assert expression == f"http.url = '{PUBLIC_HEALTH_URL}'", (
        f"public health alert filter drifted: {expression!r}; it must match "
        f"exactly {PUBLIC_HEALTH_URL}, not the root page or another probe"
    )
    aggregation = query["aggregations"][0]
    assert aggregation["metricName"] == "httpcheck.status"
    # max space aggregation avoids stale-series false positives from previous
    # collector incarnations; every existing httpcheck alert relies on it.
    assert aggregation["spaceAggregation"] == "max"


def test_public_health_alert_fires_only_on_real_http_failure():
    """op=2/target=1 means status < 1: any non-2xx probe counts as failure."""
    _, alert = _alert_json(PUBLIC_HEALTH_ALERT)
    condition = alert["condition"]
    assert condition["op"] == "2"  # less than
    assert float(condition["target"]) == 1.0


def test_public_health_alert_requires_sustained_failure_not_a_blip():
    """matchType 5 = N consecutive evaluations, N = evalWindow/frequency.

    10m window at 2m frequency means five consecutive failed probes (~10
    minutes) before firing, matching the other HTTPCheck alerts and the issue's
    suggested duration.
    """
    _, alert = _alert_json(PUBLIC_HEALTH_ALERT)
    condition = alert["condition"]
    assert condition["matchType"] == "5", (
        "matchType must be 5 (consecutive); 'once' semantics would page on a "
        "single missed probe"
    )
    window_s = _parse_duration(alert["evalWindow"])
    frequency_s = _parse_duration(alert["frequency"])
    consecutive = round(window_s / frequency_s)
    assert consecutive >= 5, (
        f"only {consecutive} consecutive evaluations required; loosen the "
        "window or raise the frequency before weakening this"
    )
    spec = condition["thresholds"]["spec"][0]
    assert spec["matchType"] == "5"
    assert float(spec["target"]) == float(condition["target"])

    # The prober cadence must line up with the evaluation frequency, else
    # "5 consecutive" counts stale data points between fresh probes.
    interval_s = _probe_interval_for(PUBLIC_HEALTH_URL)
    assert interval_s == frequency_s, (
        f"collection_interval {interval_s}s != alert frequency {frequency_s}s"
    )
    assert window_s / interval_s == consecutive


def test_public_health_alert_keys_on_status_only_so_advisory_stays_silent():
    """Advisory degradation (cd health, red CI) keeps /health at 200 + degraded[],
    which is httpcheck.status = 1 and never fires this alert. Pin that the rule
    adds no component-level condition that could re-introduce paging on 200s."""
    _, alert = _alert_json(PUBLIC_HEALTH_ALERT)
    query = alert["condition"]["compositeQuery"]["queries"][0]["spec"]
    assert query["filter"]["expression"] == f"http.url = '{PUBLIC_HEALTH_URL}'"
    assert "degraded" not in json.dumps(query), (
        "the alert must judge HTTP status alone; component-level conditions "
        "would fire on advisory degradation that /health correctly reports 200"
    )
    assert alert["disabled"] is False


def test_public_health_alert_severity_matches_platform_service_convention():
    _, alert = _alert_json(PUBLIC_HEALTH_ALERT)
    assert alert["severity"] == "critical", (
        "platform-service unreachability alerts (argocd, longhorn, signoz) are "
        "critical; demote deliberately, not by accident"
    )
    doc, _ = _alert_json(PUBLIC_HEALTH_ALERT)
    assert doc["metadata"]["annotations"]["signoz.io/severity"] == "critical"


# ---------------------------------------------------------------------------
# The probe exists and lands in the pipeline
# ---------------------------------------------------------------------------


def _values_prod() -> dict:
    return yaml.safe_load((_signoz_dir() / "values-prod.yaml").read_text())


def _httpcheck_receivers() -> dict:
    config = _values_prod()["k8s-infra"]["otelDeployment"]["config"]
    return {
        name: receiver
        for name, receiver in config["receivers"].items()
        if name.startswith("httpcheck/")
    }


def _receiver_with(endpoint: str) -> tuple[str, dict]:
    for name, receiver in _httpcheck_receivers().items():
        for target in receiver.get("targets", []):
            if target.get("endpoint") == endpoint:
                return name, receiver
    pytest.fail(f"no httpcheck receiver probes {endpoint}")


def _probe_interval_for(endpoint: str) -> float:
    name, _ = _receiver_with(endpoint)
    receiver = _httpcheck_receivers()[name]
    interval = receiver.get("collection_interval")
    return _parse_duration(interval) if interval else 60.0


def test_health_probe_endpoint_is_configured():
    # Fails with a clear message via _receiver_with if the probe is gone.
    _receiver_with(PUBLIC_HEALTH_URL)


def test_health_probe_sends_no_cloudflare_auth_headers():
    """/health is public; the CF Access service-token headers belong only to the
    private.jomcgi.dev probes. Drifting into that group would leak tokens into a
    request any observer of the public URL could trigger."""
    _, receiver = _receiver_with(PUBLIC_HEALTH_URL)
    for target in receiver["targets"]:
        if target.get("endpoint") == PUBLIC_HEALTH_URL:
            headers = target.get("headers", {})
            assert not any(k.lower().startswith("cf-access") for k in headers), (
                "public /health probe must not carry CF Access credentials"
            )
            return


def test_probe_receivers_reach_the_metrics_pipeline():
    """A receiver missing from the pipeline emits nothing; keep both groups wired."""
    pipelines = _values_prod()["k8s-infra"]["otelDeployment"]["config"]["service"][
        "pipelines"
    ]
    wired = set(pipelines["metrics/scraper"]["receivers"])
    assert {"httpcheck/k8s-services", "httpcheck/cloudflare-pages"} <= wired


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_kustomization_registers_every_alert_configmap():
    kustomization = _load_yaml("kustomization.yaml")
    resources = kustomization["resources"]
    for name in ALERT_CONFIGMAPS:
        assert name in resources, (
            f"{name} exists but is not in kustomization.yaml; it will never be "
            "applied, so the sidecar never syncs it and nothing pages"
        )
