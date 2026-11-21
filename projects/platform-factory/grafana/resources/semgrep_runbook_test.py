"""Evaluate the reviewed runbook with the original alert's URL parameters."""

import json
import os
from pathlib import Path
import runpy
import subprocess


def test_replacement_sync(tmp_path):
    app = runpy.run_path(str(Path(__file__).parent / "runbooks/server.py"))
    book = app["BOOKS"]["SemgrepSyncFailed"]
    params = {"namespace": ["factory"], "job_name": ["factory-semgrep-sync-1"]}
    check = app["bind"](book["check"], params)
    graph = app["bind"](book["graph"], params)

    def series(metric, job):
        labels = f'namespace="factory",job_name="factory-semgrep-sync-{job}"'
        if metric == "kube_job_failed":
            labels += ',condition="true"'
        return f"{metric}{{{labels}}}"

    failed_a = series("kube_job_failed", 1)
    failed_b = series("kube_job_failed", 2)
    samples = [
        (series("kube_job_status_start_time", 1), "100 100 100"),
        (series("kube_job_status_start_time", 2), "_ 200 200"),
        (series("kube_job_status_start_time", 3), "_ _ 300"),
        (failed_a, "1 1 1"),
        (failed_b, "_ 1 1"),
    ]
    cases = [
        (check, "0m", failed_a, 1),
        (check, "1m", failed_b, 1),
        (check, "2m", '{namespace="factory"}', 0),
        (graph, "2m", failed_a, 1),
    ]
    suite = {
        "interval": "1m",
        "input_series": [{"series": s, "values": v} for s, v in samples],
        "promql_expr_test": [
            {
                "expr": expr,
                "eval_time": t,
                "exp_samples": [{"labels": labels, "value": value}],
            }
            for expr, t, labels, value in cases
        ],
    }
    fixture = tmp_path / "rules.yml"
    fixture.write_text(json.dumps({"tests": [suite]}))
    subprocess.run([os.environ["PROMTOOL"], "test", "rules", fixture], check=True)
