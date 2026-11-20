"""Produce shadow forecasts from retained Prometheus history."""

import json
import math
import os
import sys
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from urllib.parse import urlencode
from urllib.request import urlopen

import numpy as np
import torch
from timestral import TimestralForecaster

STEP = 300
WINDOW = 7 * 24 * 60 * 60
HORIZONS = 6
QUERY = """
max by (namespace) (
  sum by (namespace, pod) (
    max by (namespace, pod, container) (
      container_memory_working_set_bytes{
        job="kubelet-resource", namespace="factory-r-homelab-review",
        pod=~"homelab-reviewer-.*", container!=""
      }
    )
  )
)
"""
REVIEWER_PROFILE = {
    "namespace": "factory-r-homelab-review",
    "workload": "homelab-reviewer",
    "target": QUERY,
}
MAX_SERIES = 32
DEFAULT_MIN_SAMPLES = 72


@dataclass(frozen=True)
class Point:
    labels: tuple
    policy: str
    horizon: int
    target: int
    budget: float


MODEL_REVISION = "bdd20cfc15a3fea0e4cb5ccef37f8d3ca7aa335f"


def read_history(url, end, expression):
    query = urlencode(
        {"query": expression, "start": end - WINDOW, "end": end, "step": STEP}
    )
    with urlopen(f"{url}/api/v1/query_range?{query}", timeout=20) as response:
        result = json.load(response)
    if result["status"] != "success" or result["data"]["resultType"] != "matrix":
        raise ValueError("Expected a Prometheus range query result")
    series = {}
    for row in result["data"]["result"]:
        samples = row["values"]
        if not samples or samples[-1][0] != end:
            continue
        pod = row["metric"].get("pod", "")
        if pod in series or len(series) >= MAX_SERIES:
            raise ValueError("Query must return at most one series per pod")
        series[pod] = samples
    return series


def aligned_history(samples, end):
    history = np.full(WINDOW // STEP + 1, np.nan, dtype=np.float32)
    start = end - WINDOW
    for timestamp, value in samples:
        if start <= timestamp <= end and (timestamp - start) % STEP == 0:
            history[int(timestamp - start) // STEP] = float(value)
    history[~np.isfinite(history)] = np.nan
    return history


def forecast_points(
    model, samples, end, labels, covariates=(), min_samples=DEFAULT_MIN_SAMPLES
):
    history = aligned_history(samples, end)
    if (
        not np.isfinite(history[-1])
        or np.count_nonzero(np.isfinite(history)) < min_samples
    ):
        raise ValueError("Memory history is stale or too sparse")

    past = [aligned_history(samples, end) for samples in covariates]
    past = [series for series in past if np.isfinite(series).sum() >= min_samples]
    forecast = model.forecast(
        history,
        prediction_length=HORIZONS,
        past_covariates=np.stack(past)[None, :, :] if past else None,
    )
    baseline = np.nanpercentile(history[-288:], 90)
    points = []
    for policy, budgets in (
        ("timestral", forecast.quantile(0.9).reshape(-1)),
        ("baseline", [baseline] * HORIZONS),
    ):
        for i, budget in enumerate(budgets, start=1):
            if math.isfinite(budget):
                horizon = STEP * i
                points.append(
                    Point(labels, policy, horizon, end + horizon, max(0, float(budget)))
                )
    return tuple(points)


def render_metrics(points, now):
    lines = [
        "# HELP capacity_forecast_memory_budget_bytes Predicted pod memory budget.",
        "# TYPE capacity_forecast_memory_budget_bytes gauge",
        "# HELP capacity_forecast_target_timestamp_seconds Time the prediction applies to.",
        "# TYPE capacity_forecast_target_timestamp_seconds gauge",
    ]
    for point in points:
        if point.target > now:
            labels = (
                *point.labels,
                ("policy", point.policy),
                ("horizon_seconds", point.horizon),
            )
            labelset = ",".join(
                f"{key}={json.dumps(str(value), ensure_ascii=False)}"
                for key, value in labels
            )
            lines.append(
                f"capacity_forecast_memory_budget_bytes{{{labelset}}} {point.budget}"
            )
            lines.append(
                f"capacity_forecast_target_timestamp_seconds{{{labelset}}} {point.target}"
            )
    return "\n".join(lines) + "\n"


def profile_labels(profile, pod=""):
    labels = (("namespace", profile["namespace"]), ("workload", profile["workload"]))
    return (*labels, ("pod", pod)) if pod else labels


def collect_profiles(model, url, profiles, end):
    points = []
    for profile in profiles:
        try:
            targets = read_history(url, end, profile["target"])
            signals = []
            for expression in profile.get("past_covariates", []):
                try:
                    signals.append(read_history(url, end, expression))
                except (OSError, ValueError, RuntimeError) as error:
                    print(
                        f"Optional signal unavailable: {error}",
                        file=sys.stderr,
                        flush=True,
                    )
            for pod, samples in targets.items():
                try:
                    covariates = [
                        signal.get(pod, signal.get("", [])) for signal in signals
                    ]
                    points.extend(
                        forecast_points(
                            model,
                            samples,
                            end,
                            profile_labels(profile, pod),
                            covariates,
                            profile.get("min_samples", DEFAULT_MIN_SAMPLES),
                        )
                    )
                except (ValueError, RuntimeError) as error:
                    print(
                        f"Forecast unavailable for {profile['workload']}/{pod}: {error}",
                        file=sys.stderr,
                        flush=True,
                    )
        except (OSError, ValueError, RuntimeError) as error:
            print(
                f"Profile unavailable for {profile['workload']}: {error}",
                file=sys.stderr,
                flush=True,
            )
    return tuple(points)


class MetricsHandler(BaseHTTPRequestHandler):
    points = ()

    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(render_metrics(self.points, time.time()).encode())


def main():
    torch.set_num_threads(2)
    model_dir = os.environ.get("TIMESTRAL_MODEL_DIR")
    model = TimestralForecaster.from_pretrained(
        model_dir or "mistralai/Timestral-5M",
        revision=MODEL_REVISION,
        local_files_only=True,
    )
    if len(sys.argv) in (2, 3) and sys.argv[1] == "--replay":
        replay = json.load(sys.stdin)
        samples = replay["data"]["result"][0]["values"]
        end = int(samples[-1][0])
        now = int(sys.argv[2]) if len(sys.argv) == 3 else end
        points = forecast_points(
            model,
            samples,
            end,
            profile_labels(REVIEWER_PROFILE),
            replay.get("past_covariates", []),
            replay.get("min_samples", DEFAULT_MIN_SAMPLES),
        )
        print(render_metrics(points, now), end="")
        return

    url = os.environ["PROMETHEUS_URL"].rstrip("/")
    profiles = (
        json.loads(os.environ["FORECAST_PROFILES"])
        if "FORECAST_PROFILES" in os.environ
        else [REVIEWER_PROFILE]
    )
    identities = {(profile["namespace"], profile["workload"]) for profile in profiles}
    if len(identities) != len(profiles):
        raise ValueError("Forecast profile namespace and workload must be unique")
    for profile in profiles:
        minimum = profile.get("min_samples", DEFAULT_MIN_SAMPLES)
        if type(minimum) is not int or not 6 <= minimum <= WINDOW // STEP + 1:
            raise ValueError("Profile min_samples must be between 6 and 2017")
    server = HTTPServer(("0.0.0.0", 8080), MetricsHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    while True:
        # Leave time for the scrapes that feed the latest evaluation.
        end = (int(time.time()) - 35) // STEP * STEP
        MetricsHandler.points = collect_profiles(model, url, profiles, end)
        time.sleep(STEP - (time.time() - 35) % STEP)


if __name__ == "__main__":
    main()
