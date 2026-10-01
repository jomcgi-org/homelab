"""Production-shaped digest-only publishes must leave every brick pod unchanged."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import yaml


CHART = Path(__file__).resolve().parent
DEPLOY = Path(os.environ.get("DEPLOY_VALUES", CHART.parent / "deploy/values.yaml"))
GKE = Path(os.environ.get("GKE_VALUES", CHART.parent / "deploy/values-gke.yaml"))


def _merge(left, right):
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(left.get(key), dict):
            _merge(left[key], value)
        else:
            left[key] = value
    return left


def _render(tmp_path, seed, enabled, daemonset=False):
    merged = {}
    for source in [CHART / "values.yaml", DEPLOY, GKE]:
        _merge(merged, yaml.safe_load(source.read_text()))
    digests = {
        key: "sha256:" + hashlib.sha256(f"{seed}:{key}".encode()).hexdigest()
        for key in merged["workloads"]
    }
    overlay = {key: {"guestImage": {"digest": digest}} for key, digest in digests.items()}
    overlay["rootfsBuilder"] = {"inPodBake": {"enabled": enabled}}
    if daemonset:
        overlay["bricks"] = {"enabled": False}
        overlay["noded"] = {"enabled": True}
    else:
        # GKE currently has no floors. Exercise the production floor template
        # with a real class and the supported anchor-selector placement shape.
        overlay["bricks"] = {"nodeFloors": [{
            "name": "anchor", "selector": {"homelab.io/anchor": "true"},
            "class": merged["bricks"]["classes"][0]["name"],
        }]}
    file = tmp_path / f"{seed}-{enabled}-{daemonset}.yaml"
    file.write_text(yaml.safe_dump(overlay))
    result = subprocess.run(
        [os.environ.get("HELM_BIN", "helm"), "template", "embervm", str(CHART),
         "-f", str(DEPLOY), "-f", str(GKE), "-f", str(file)],
        check=True, text=True, capture_output=True,
    )
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    pods = {
        doc["metadata"]["name"]: doc["spec"]["template"]
        for doc in docs
        if (doc["kind"] == "Deployment" and "-noded-brick-" in doc["metadata"]["name"])
        or (doc["kind"] == "DaemonSet" and doc["metadata"]["name"].endswith("-noded"))
    }
    cm = next(doc for doc in docs if doc["kind"] == "ConfigMap"
              and doc["metadata"]["name"].endswith("-rootfs-builder"))
    return pods, cm, digests


def test_digest_only_publish_hot_swaps_all_bricks_and_floors(tmp_path):
    for daemonset in [False, True]:
        a, cm_a, digests_a = _render(tmp_path, "A", True, daemonset)
        b, cm_b, digests_b = _render(tmp_path, "B", True, daemonset)
        assert a and a == b
        if not daemonset:
            assert any("-anchor" in name for name in a), "production floor must render"
            assert len(a) > 1, "production classes must render"
        serialized = json.dumps(a)
        for digest in [*digests_a.values(), *digests_b.values()]:
            assert digest not in serialized
        assert cm_a != cm_b
        for cm, digests in [(cm_a, digests_a), (cm_b, digests_b)]:
            for digest in digests.values():
                assert digest in cm["data"]["rootfs-desired-set"]
        for pod in a.values():
            spec = pod["spec"]
            assert "rootfs-baker" in [c["name"] for c in spec["containers"]]
            assert not any(c["name"].startswith("build-") for c in spec.get("initContainers", []))
        old_a, _, _ = _render(tmp_path, "A", False, daemonset)
        old_b, _, _ = _render(tmp_path, "B", False, daemonset)
        assert old_a.keys() == old_b.keys() == a.keys()
        assert all(old_a[name] != old_b[name] for name in a), "negative control must roll every pod"


def test_flag_on_without_scratch_gates_omits_empty_init_containers(tmp_path):
    result = subprocess.run(
        [os.environ.get("HELM_BIN", "helm"), "template", "embervm", str(CHART),
         "--set", "rootfsBuilder.inPodBake.enabled=true"],
        check=True, text=True, capture_output=True,
    )
    pod = next(doc["spec"]["template"]["spec"] for doc in yaml.safe_load_all(result.stdout)
               if doc and doc["kind"] == "DaemonSet" and doc["metadata"]["name"].endswith("-noded"))
    assert "initContainers" not in pod
