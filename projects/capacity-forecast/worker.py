"""Produce shadow forecasts from retained Prometheus history."""

import json
import math
import os
import sys
import time
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
LABELS = 'namespace="factory-r-homelab-review",workload="homelab-reviewer"'
MODEL_REVISION = "bdd20cfc15a3fea0e4cb5ccef37f8d3ca7aa335f"


def read_history(url, end):
    query = urlencode({"query": QUERY, "start": end - WINDOW, "end": end, "step": STEP})
    with urlopen(f"{url}/api/v1/query_range?{query}", timeout=20) as response:
        result = json.load(response)
    if result["status"] != "success" or len(result["data"]["result"]) != 1:
        raise ValueError("Expected one reviewer memory series")
    return result["data"]["result"][0]["values"]


def forecast_metrics(model, samples, end, now):
    history = np.full(WINDOW // STEP + 1, np.nan, dtype=np.float32)
    start = end - WINDOW
    for timestamp, value in samples:
        if start <= timestamp <= end and (timestamp - start) % STEP == 0:
            history[int(timestamp - start) // STEP] = float(value)
    history[~np.isfinite(history)] = np.nan
    if not np.isfinite(history[-1]) or np.count_nonzero(np.isfinite(history)) < 72:
        raise ValueError("Reviewer history is stale or too sparse")

    forecast = model.forecast(history, prediction_length=HORIZONS)
    baseline = np.nanpercentile(history[-288:], 90)
    lines = [
        "# HELP capacity_forecast_memory_budget_bytes Predicted reviewer pod memory budget.",
        "# TYPE capacity_forecast_memory_budget_bytes gauge",
        "# HELP capacity_forecast_target_timestamp_seconds Time the prediction applies to.",
        "# TYPE capacity_forecast_target_timestamp_seconds gauge",
    ]
    for policy, budgets in (
        ("timestral", forecast.quantile(0.9).reshape(-1)),
        ("baseline", [baseline] * HORIZONS),
    ):
        for i, budget in enumerate(budgets, start=1):
            target = end + STEP * i
            if target > now and math.isfinite(budget):
                labels = f'{LABELS},policy="{policy}",horizon_seconds="{STEP * i}"'
                lines.append(
                    f"capacity_forecast_memory_budget_bytes{{{labels}}} {max(0, float(budget))}"
                )
                lines.append(
                    f"capacity_forecast_target_timestamp_seconds{{{labels}}} {target}"
                )
    return "\n".join(lines) + "\n"


class MetricsHandler(BaseHTTPRequestHandler):
    metrics = b""

    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(self.metrics)


def main():
    torch.set_num_threads(2)
    model_dir = os.environ.get("TIMESTRAL_MODEL_DIR")
    model = TimestralForecaster.from_pretrained(
        model_dir or "mistralai/Timestral-5M",
        revision=MODEL_REVISION,
        local_files_only=True,
    )
    if len(sys.argv) == 2 and sys.argv[1] == "--replay":
        samples = json.load(sys.stdin)["data"]["result"][0]["values"]
        end = int(samples[-1][0])
        print(forecast_metrics(model, samples, end, end))
        return

    url = os.environ["PROMETHEUS_URL"].rstrip("/")
    server = HTTPServer(("0.0.0.0", 8080), MetricsHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    while True:
        try:
            # Leave time for the scrapes that feed the latest evaluation.
            now = int(time.time())
            end = (now - 35) // STEP * STEP
            samples = read_history(url, end)
            MetricsHandler.metrics = forecast_metrics(
                model, samples, end, int(time.time())
            ).encode()
        except (OSError, ValueError, RuntimeError) as error:
            MetricsHandler.metrics = b""
            print(f"Forecast unavailable: {error}", file=sys.stderr, flush=True)
        time.sleep(STEP - (time.time() - 35) % STEP)


if __name__ == "__main__":
    main()
