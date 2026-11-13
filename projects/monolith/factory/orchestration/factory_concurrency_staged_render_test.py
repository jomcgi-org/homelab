"""Render the opt-in package using the pinned Helm in Linux CI."""

import json
import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]


def render(service, staged):
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        service,
        str(ROOT / f"projects/{service}/chart"),
        "--namespace",
        service,
    ]
    for name in ["values.yaml", "values-gke.yaml"] + (
        ["values-gke-concurrency-staged.yaml"] if staged else []
    ):
        argv += ["-f", str(ROOT / f"projects/{service}/deploy/{name}")]
    return [
        d for d in yaml.safe_load_all(subprocess.check_output(argv, text=True)) if d
    ]


def containers(docs):
    return [
        c
        for d in docs
        for c in d.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
    ]


def env(container):
    return {e["name"]: e.get("value") for e in container.get("env", [])}


def test_opt_in_admission_and_workload():
    for staged, limits in [(False, (12, 24, 20)), (True, (48, 64, 56))]:
        docs = render("monolith", staged)
        settings = next(
            env(c) for c in containers(docs) if "FACTORY_MAX_CONCURRENT_TASKS" in env(c)
        )
        assert (
            tuple(
                int(settings[k])
                for k in [
                    "FACTORY_MAX_CONCURRENT_TASKS",
                    "AGENT_ADMISSION_TOTAL",
                    "AGENT_ADMISSION_BACKGROUND",
                ]
            )
            == limits
        )
        docs = render("embervm", staged)
        workload = next(
            d["spec"]
            for d in docs
            if d["kind"] == "Workload"
            and d["metadata"]["name"].endswith("claude-runtime")
        )
        assert workload["resources"] == {"vcpus": 2, "memMib": 3072}
        assert workload["concurrency"]["cap"] == (64 if staged else 24)
        assert workload["session"]["maxSessions"] == (96 if staged else 32)
        settings = next(
            env(c) for c in containers(docs) if "EMBERVM_BRICK_CLASSES" in env(c)
        )
        classes = {c["name"]: c for c in json.loads(settings["EMBERVM_BRICK_CLASSES"])}
        assert {k: classes[k]["max"] for k in ["2gi", "4gi", "8gi", "16gi"]} == {
            "2gi": 4,
            "4gi": 4 if staged else 2,
            "8gi": 32 if staged else 3,
            "16gi": 0,
        }
        assert all(c["ceiling_bound"] == 0 for c in classes.values())
        assert all(
            env(c)["EMBERVM_NODED_ADMISSION_MODEL"] == "observed"
            for c in containers(docs)
            if c["name"] == "noded"
        )
        if staged:
            # Conservative declared-memory capacity, without assuming observed packing.
            assert (
                classes["4gi"]["usable_mib"] // (3072 + 64),
                classes["8gi"]["usable_mib"] // (3072 + 64),
            ) == (1, 2)
            assert classes["4gi"]["max"] + 2 * classes["8gi"]["max"] == 68
            bricks = [c for c in containers(docs) if c["name"] == "noded"]
            assert bricks
            assert all(
                c["resources"]["requests"]["memory"]
                == c["resources"]["limits"]["memory"]
                for c in bricks
            )


def test_node_packing_bound():
    docs = render("embervm", True)
    pods = {
        d["metadata"]["name"].rsplit("-", 1)[-1]: d["spec"]["template"]["spec"]
        for d in docs
        if d["kind"] == "Deployment" and "-noded-brick-" in d["metadata"]["name"]
    }

    def requests(kind):
        cpu, memory = 0, 0
        for container in pods[kind]["containers"]:
            request = container["resources"]["requests"]
            c, m = str(request["cpu"]), request["memory"]
            cpu += int(c[:-1]) if c.endswith("m") else int(c) * 1000
            memory += int(m[:-2]) * (1024 if m.endswith("Gi") else 1)
        return cpu, memory

    assert requests("8gi") == (3110, 8384)
    assert requests("4gi") == (2110, 4288)
    assert requests("2gi") == (1110, 2240)
    cpu8, mem8 = requests("8gi")
    cpu2, mem2 = requests("2gi")
    cpu4, mem4 = requests("4gi")
    # Reserve 1000m/1Gi for DaemonSets below planning allocatable.
    assert 2 * cpu8 <= 7900 - 1000
    assert 2 * mem8 <= (27.5 - 1) * 1024
    # Two additional nodes each carry two 4gi and two 2gi.
    assert 2 * cpu4 + 2 * cpu2 <= 7900 - 1000
    assert 2 * mem4 + 2 * mem2 <= (27.5 - 1) * 1024
    assert 32 * cpu8 + 4 * cpu4 + 4 * cpu2 == 112400
    assert 32 * mem8 + 4 * mem4 + 4 * mem2 == 294400
    assert 16 + 2 + 1 + 2 == 21  # capacity, shared clients, replacement/rollout
