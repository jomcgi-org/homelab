import os
from pathlib import Path
import subprocess

import yaml


def test_writer_without_mirror():
    cmd = [
        os.environ["HELM_BIN"],
        "template",
        "stars-forecast",
        str(Path(__file__).parent),
        "--namespace",
        "stars-forecast",
        "--set",
        "image.digest=sha256:" + "a" * 64,
    ]
    for enabled in [False, True]:
        docs = list(
            yaml.safe_load_all(
                subprocess.check_output(
                    cmd + ["--set", f"refreshEnabled={str(enabled).lower()}"]
                )
            )
        )
        pull = next(
            d
            for d in docs
            if d
            and d["kind"] == "OnePasswordItem"
            and d["metadata"]["name"] == "ghcr-imagepull-secret"
        )
        assert pull["type"] == "kubernetes.io/dockerconfigjson"
        jobs = {
            d["metadata"]["name"]: d["spec"]
            for d in docs
            if d and d["kind"] == "CronJob"
        }
        assert jobs["grid"]["schedule"] == "0 */6 * * *"
        assert ("refresh" in jobs) == enabled
        assert "prune" not in jobs
        hooks = {d["metadata"]["name"]: d for d in docs if d and d["kind"] == "Job"}
        assert ("inputs" in hooks) == enabled
        if enabled:
            # met.no budget: 50 minutes after the hub's stars-refresh.
            assert jobs["refresh"]["schedule"] == "50 */3 * * *"
            assert jobs["refresh"]["concurrencyPolicy"] == "Forbid"
            # The inputs hook imports grid and climatology in wave 0; the
            # refresh CronJob syncs after it, in wave 1.
            refresh = next(
                d
                for d in docs
                if d and d["kind"] == "CronJob" and d["metadata"]["name"] == "refresh"
            )
            assert (
                refresh["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]
                == "1"
            )
            meta = hooks["inputs"]["metadata"]["annotations"]
            assert meta["argocd.argoproj.io/hook"] == "Sync"
            assert meta["argocd.argoproj.io/sync-wave"] == "0"
            container = hooks["inputs"]["spec"]["template"]["spec"]["containers"][0]
            assert container["args"] == ["inputs"]
        # The factory-public release mirrors the bucket (HOME-492). This
        # namespace holds no R2 write credential and runs no mirror.
        assert "stars-forecast-stars-forecast-mirror" not in jobs
        rendered = yaml.safe_dump_all(docs)
        assert "R2_BUCKET" not in rendered
        assert "factory-r2-public-mirror" not in rendered
