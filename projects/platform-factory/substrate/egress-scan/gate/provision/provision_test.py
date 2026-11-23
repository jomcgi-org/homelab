"""Validate the staged engine provisioning: the template says what engine-gate
needs, the script is idempotent and removes what it made, and the Jobs are
suspended, unprivileged and limited to the ate API."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
_PRO_IMAGE = (
    "ghcr.io/jomcgi-org/homelab/substrate-fork/engine-pro:1.186.0-0adcac4b1419"
    "@sha256:2235edd728528d00ae623be2200d9c29e44e8015300ad2368f7a303b4492f481"
)
_EDITOR = "user:system:serviceaccount:ate-system:engine-gate"


def _load():
    spec = importlib.util.spec_from_file_location("provision", _HERE / "provision.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.POLL = 0
    return mod


def _docs() -> list[dict]:
    out = subprocess.run(
        [os.environ["KUSTOMIZE_BIN"], "build", str(_HERE)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _one(kind: str, name: str) -> dict:
    (found,) = [
        d for d in _docs() if d["kind"] == kind and d["metadata"]["name"] == name
    ]
    return found


_TEMPLATE = (_HERE / "template.yaml").read_text()


# --- the template -----------------------------------------------------------


def test_template_is_the_pro_image_by_digest() -> None:
    t = yaml.safe_load(_TEMPLATE)
    (c,) = t["containers"]
    assert c["image"] == _PRO_IMAGE
    assert t["metadata"] == {"atespace": "egress-scan-engine", "name": "engine"}


def test_template_listens_on_port_80_with_no_env_and_no_egress() -> None:
    t = yaml.safe_load(_TEMPLATE)
    (c,) = t["containers"]
    assert c["command"] == ["/usr/local/bin/forksrv-net", "--listen", "0.0.0.0:80"]
    assert c["wakeupProbe"]["httpGet"] == {"path": "/healthz", "port": 80}
    # The API refuses an env named like a secret, and every child reads it. The
    # placeholder token is a file in the image.
    assert "env" not in c
    fork = t["forkConfig"]
    assert fork["mode"] == "FORK_MODE_SEED_AND_FORK"
    assert fork["singleUse"] is True
    assert fork["egress"] == "FORK_EGRESS_NONE"
    assert fork["maxLifetimeSeconds"] == 900
    assert fork["seedResources"]["limits"] == {
        "cpuMillicores": 1000,
        "memoryBytes": 512 << 20,
    }
    assert t["resources"]["limits"] == {"cpuMillicores": 1000, "memoryBytes": 128 << 20}
    assert t["workerSelector"] == {"matchLabels": {"workload": "gvisor-fork"}}


def test_template_checks_pass_and_catch_each_edit() -> None:
    mod = _load()
    assert mod.template_checks(_TEMPLATE) == []
    edits = {
        "image digest": (_PRO_IMAGE.split("@")[1], "sha256:" + "0" * 64),
        "port": ("0.0.0.0:80", "0.0.0.0:8080"),
        "probe": ("port: 80", "port: 8080"),
        "single use": ("singleUse: true", "singleUse: false"),
        "egress": ("egress: FORK_EGRESS_NONE", "egress: FORK_EGRESS_GATEWAY"),
        "lifetime": ("maxLifetimeSeconds: 900", "maxLifetimeSeconds: 300"),
        "atespace": ("atespace: egress-scan-engine", "atespace: other"),
        "env": (
            "    wakeupProbe:",
            "    env:\n      - name: X\n        value: y\n    wakeupProbe:",
        ),
    }
    for name, (old, new) in edits.items():
        assert old in _TEMPLATE, name
        assert mod.template_checks(_TEMPLATE.replace(old, new, 1)), name


def test_template_names_no_secret_and_no_token() -> None:
    body = re.sub(r"(?m)^\s*#.*$", "", _TEMPLATE)
    assert not re.search(r"(?i)token|secret|password|key", body)


# --- the script, without a cluster ------------------------------------------


def test_editor_member_has_the_user_prefix() -> None:
    mod = _load()
    assert mod.EDITOR == _EDITOR


def test_policy_with_editor() -> None:
    mod = _load()
    manifest, changed = mod.policy_with_editor(None, _EDITOR)
    assert changed and manifest == {
        "bindings": [{"role": "editor", "members": [_EDITOR]}]
    }
    # Already there: no change.
    cur = {
        "metadata": {"uid": "u1", "version": "3"},
        "bindings": [{"role": "editor", "members": [_EDITOR]}],
    }
    assert mod.policy_with_editor(cur, _EDITOR)[1] is False
    # Other bindings and members stay, and the update carries its preconditions.
    cur = {
        "metadata": {"uid": "u1", "version": "3"},
        "bindings": [
            {"role": "owner", "members": ["user:alice"]},
            {"role": "editor", "members": ["user:bob"]},
        ],
    }
    manifest, changed = mod.policy_with_editor(cur, _EDITOR)
    assert changed
    assert manifest["metadata"] == {"uid": "u1", "version": "3"}
    assert manifest["bindings"] == [
        {"role": "owner", "members": ["user:alice"]},
        {"role": "editor", "members": ["user:bob", _EDITOR]},
    ]


class FakeAte:
    """A tiny kubectl-ate: atespaces, one policy, templates, actors, a worker."""

    def __init__(self) -> None:
        self.atespaces: set[str] = set()
        self.policy: dict | None = None
        self.templates: dict[str, dict] = {}
        self.actors: list[dict] = []
        self.calls: list[tuple[str, ...]] = []
        self.worker_ready = True
        self.version = 0

    def out(self, obj: object, code: int = 0):
        return subprocess.CompletedProcess([], code, json.dumps(obj), "")

    def __call__(self, *args: str):
        self.calls.append(args)
        verb, kind = args[0], args[1]
        rest = list(args[2:])
        atespace = rest[rest.index("-a") + 1] if "-a" in rest else None
        if verb == "get" and kind == "workers":
            seeds = [
                {
                    "seedActor": {"atespace": "egress-scan-engine"},
                    "templateUid": t["metadata"]["uid"],
                }
                for t in self.templates.values()
            ]
            state = (
                "WORKER_STATE_ACTIVE" if self.worker_ready else "WORKER_STATE_STARTING"
            )
            return self.out(
                [{"workerPool": "gvisor-fork", "state": state, "seeds": seeds}]
            )
        if verb == "get" and kind == "atespace":
            return self.out({}, 0 if rest[0] in self.atespaces else 1)
        if verb == "create" and kind == "atespace":
            self.atespaces.add(rest[0])
            return self.out({})
        if verb == "get" and kind == "access-policy":
            return (
                self.out(self.policy)
                if self.policy
                else subprocess.CompletedProcess([], 0, "", "no access policy")
            )
        if verb in ("create", "update") and kind == "access-policy":
            body = json.loads(Path(rest[rest.index("-f") + 1]).read_text())
            self.version += 1
            self.policy = {
                "metadata": {"uid": "pol-1", "version": str(self.version)},
                **body,
            }
            return self.out({})
        if verb == "delete" and kind == "access-policy":
            self.policy = None
            return self.out({})
        if verb == "get" and kind == "actor-template":
            if len(rest) > 0 and not rest[0].startswith("-"):
                t = self.templates.get(rest[0])
                return self.out(t, 0) if t else self.out({}, 1)
            return self.out(list(self.templates.values()))
        if verb == "create" and kind == "actor-template":
            text = Path(rest[rest.index("-f") + 1]).read_text()
            t = yaml.safe_load(text)
            t["metadata"]["uid"] = f"uid-{len(self.templates) + 1}"
            self.templates[t["metadata"]["name"]] = t
            return self.out({})
        if verb == "delete" and kind == "actor-template":
            self.templates.pop(rest[0], None)
            return self.out({})
        if verb == "get" and kind == "actors":
            return self.out(self.actors)
        if verb == "delete" and kind == "actor":
            self.actors = [a for a in self.actors if a["metadata"]["name"] != rest[0]]
            return self.out({})
        if verb == "delete" and kind == "atespace":
            self.atespaces.discard(rest[0])
            return self.out({})
        raise AssertionError(f"unexpected call {args}")

    def mutations(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[0] in ("create", "update", "delete")]


def _run(mod, fake: FakeAte, tmp_path: Path, mode: str = "apply"):
    mod.ate = fake
    mod.MODE = mode
    mod.TEMPLATE_FILE = _HERE / "template.yaml"
    mod.WORK_DIR = tmp_path
    os.environ["WORK_DIR"] = str(tmp_path)
    return mod.main()


def test_apply_creates_then_a_second_run_changes_nothing(tmp_path: Path) -> None:
    mod, fake = _load(), FakeAte()
    assert _run(mod, fake, tmp_path) == 0
    assert fake.atespaces == {"egress-scan-engine"}
    assert fake.policy["bindings"] == [{"role": "editor", "members": [_EDITOR]}]
    assert list(fake.templates) == ["engine"]
    first = [c[:3] for c in fake.mutations()]
    assert first == [
        ("create", "atespace", "egress-scan-engine"),
        ("create", "access-policy", "-a"),
        ("create", "actor-template", "-f"),
    ]
    fake.calls.clear()
    assert _run(mod, fake, tmp_path) == 0
    assert fake.mutations() == [], "a second run must change nothing"


def test_apply_adds_the_editor_to_an_existing_policy(tmp_path: Path) -> None:
    mod, fake = _load(), FakeAte()
    fake.atespaces.add("egress-scan-engine")
    fake.policy = {
        "metadata": {"uid": "pol-1", "version": "7"},
        "bindings": [{"role": "owner", "members": ["user:alice"]}],
    }
    assert _run(mod, fake, tmp_path) == 0
    assert fake.policy["bindings"] == [
        {"role": "owner", "members": ["user:alice"]},
        {"role": "editor", "members": [_EDITOR]},
    ]
    assert ("update", "access-policy") in [c[:2] for c in fake.mutations()]


def test_apply_refuses_to_edit_a_template_in_place(tmp_path: Path) -> None:
    mod, fake = _load(), FakeAte()
    fake.atespaces.add("egress-scan-engine")
    fake.templates["engine"] = {
        "metadata": {"name": "engine", "uid": "uid-9"},
        "containers": [{"image": "ghcr.io/x/engine@sha256:" + "a" * 64}],
    }
    assert _run(mod, fake, tmp_path) == 1
    assert not [c for c in fake.mutations() if c[1] == "actor-template"]


def test_apply_makes_no_call_for_a_bad_template(tmp_path: Path) -> None:
    mod, fake = _load(), FakeAte()
    bad = tmp_path / "template.yaml"
    bad.write_text(_TEMPLATE.replace("0.0.0.0:80", "0.0.0.0:8080"))
    mod.ate = fake
    mod.MODE = "apply"
    mod.TEMPLATE_FILE = bad
    assert mod.main() == 1
    assert fake.calls == []


def test_teardown_removes_everything_in_order_and_is_safe_twice(tmp_path: Path) -> None:
    mod, fake = _load(), FakeAte()
    assert _run(mod, fake, tmp_path) == 0
    fake.actors = [
        {
            "metadata": {"name": "eg-engine-gate-0-1-1"},
            "status": {
                "state": "ACTOR_STATE_RUNNING",
                "fork": {"role": "FORK_ROLE_FORK"},
            },
        },
        {
            "metadata": {"name": "seed-abc"},
            "status": {
                "state": "ACTOR_STATE_RUNNING",
                "fork": {"role": "FORK_ROLE_SEED"},
            },
        },
    ]
    fake.calls.clear()
    # The seed goes with its template, as in the API.
    orig = fake.__call__

    def with_seed_removal(*args: str):
        out = orig(*args)
        if args[:2] == ("delete", "actor-template"):
            fake.actors = [
                a for a in fake.actors if a["metadata"]["name"] != "seed-abc"
            ]
        return out

    assert _run_teardown(mod, with_seed_removal, tmp_path) == 0
    order = [c[:2] for c in fake.mutations()]
    assert order == [
        ("delete", "actor"),
        ("delete", "actor-template"),
        ("delete", "access-policy"),
        ("delete", "atespace"),
    ]
    assert not fake.atespaces and fake.policy is None and not fake.templates
    # The seed actor itself is never deleted by name.
    assert ("delete", "actor", "seed-abc") not in [c[:3] for c in fake.calls]
    fake.calls.clear()
    assert _run_teardown(mod, with_seed_removal, tmp_path) == 0
    assert fake.mutations() == []


def _run_teardown(mod, ate, tmp_path: Path):
    mod.ate = ate
    mod.MODE = "teardown"
    return mod.main()


# --- the manifests ----------------------------------------------------------


def test_both_jobs_are_suspended_and_use_the_ate_client() -> None:
    for name, mode in (
        ("egress-engine-provision-1", "apply"),
        ("egress-engine-teardown-1", "teardown"),
    ):
        job = _one("Job", name)
        assert job["spec"]["suspend"] is True
        assert job["spec"]["backoffLimit"] == 0
        pod = job["spec"]["template"]["spec"]
        assert pod["serviceAccountName"] == "ate-client"
        assert pod["restartPolicy"] == "Never"
        (c,) = pod["containers"]
        assert {e["name"]: e["value"] for e in c["env"]}["MODE"] == mode
        assert c["command"] == ["python3", "-I", "/config/provision.py"]
        assert job["metadata"]["namespace"] == "ate-system"


def test_jobs_are_unprivileged_and_hold_no_secret() -> None:
    for name in ("egress-engine-provision-1", "egress-engine-teardown-1"):
        pod = _one("Job", name)["spec"]["template"]["spec"]
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
        (c,) = pod["containers"]
        sc = c["securityContext"]
        assert sc["allowPrivilegeEscalation"] is False
        assert sc["readOnlyRootFilesystem"] is True
        assert sc["capabilities"]["drop"] == ["ALL"]
        assert "envFrom" not in c
        assert not [v for v in pod["volumes"] if "secret" in v]
        assert not re.search(r"(?i)token|secret|password", json.dumps(c["env"]))
        token = [v for v in pod["volumes"] if v["name"] == "ate-token"][0]["projected"][
            "sources"
        ][0]
        assert token["serviceAccountToken"]["audience"] == "api.ate-system.svc"
        # Images are pinned by digest.
        assert "@sha256:" in c["image"]
        cli = [v for v in pod["volumes"] if v["name"] == "cli"][0]
        assert "@sha256:" in cli["image"]["reference"]


def test_config_map_holds_the_script_and_the_template() -> None:
    (cm,) = [d for d in _docs() if d["kind"] == "ConfigMap"]
    assert set(cm["data"]) == {"provision.py", "template.yaml"}
    assert cm["data"]["template.yaml"] == _TEMPLATE
    for name in ("egress-engine-provision-1", "egress-engine-teardown-1"):
        vol = [
            v
            for v in _one("Job", name)["spec"]["template"]["spec"]["volumes"]
            if v["name"] == "config"
        ][0]
        assert vol["configMap"]["name"] == cm["metadata"]["name"]


def test_jobs_reach_only_the_kubernetes_api_and_the_ate_api() -> None:
    policy = _one("CiliumNetworkPolicy", "egress-engine-provision")["spec"]
    assert policy["endpointSelector"]["matchLabels"] == {
        "app": "egress-engine-provision"
    }
    assert policy["egress"] == [
        {
            "toEntities": ["kube-apiserver"],
            "toPorts": [{"ports": [{"port": "6443", "protocol": "TCP"}]}],
        },
        {
            "toEndpoints": [{"matchLabels": {"app": "ate-api-server"}}],
            "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}],
        },
    ]
    api = _one("CiliumNetworkPolicy", "ate-api-server-egress-engine-provision")["spec"]
    (rule,) = api["ingress"]
    assert rule["fromEndpoints"] == [
        {"matchLabels": {"app": "egress-engine-provision"}}
    ]
    assert rule["toPorts"][0]["ports"] == [{"port": "443", "protocol": "TCP"}]
    for name in ("egress-engine-provision-1", "egress-engine-teardown-1"):
        labels = _one("Job", name)["spec"]["template"]["metadata"]["labels"]
        assert labels == {"app": "egress-engine-provision"}
