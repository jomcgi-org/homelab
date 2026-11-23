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


_SECRET_MARKERS = ("TOKEN", "KEY", "SECRET", "PASSWORD")
_SHORT_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def fork_rule_violations(t: dict) -> list[str]:
    """Every rule the ate API applies to a template with fork_config.

    Source: the fork's cmd/ateapi/internal/apivalidation/fork.go
    (validateForkTemplate and the seed resources check) and the declarative
    validation tags of ForkConfig, ActorTemplate, Container and WakeupProbe in
    api/ateapi/v1/ateapi.proto, at fork commit 1e7a877d0ca3.
    """
    bad = []
    fork = t.get("forkConfig")
    if not fork:
        return ["no forkConfig"]
    if fork.get("mode") != "FORK_MODE_SEED_AND_FORK":
        bad.append("forkConfig.mode must be FORK_MODE_SEED_AND_FORK")
    seed = (fork.get("seedResources") or {}).get("limits") or {}
    if not seed.get("cpuMillicores") or not seed.get("memoryBytes"):
        bad.append("forkConfig.seedResources.limits needs cpu and memory")
    if fork.get("singleUse") is not True:
        bad.append("forkConfig.singleUse must be true")
    if not 1 <= fork.get("maxLifetimeSeconds", 0) <= 3600:
        bad.append("forkConfig.maxLifetimeSeconds must be 1 to 3600")
    if not 32 <= fork.get("pidsLimit", 0) <= 4096:
        bad.append("forkConfig.pidsLimit must be 32 to 4096")
    if not 1024 <= int(fork.get("logBytesLimit", 0)) <= 1 << 30:
        bad.append("forkConfig.logBytesLimit must be 1024 to 1 GiB")
    if fork.get("egress", "FORK_EGRESS_NONE") not in (
        "FORK_EGRESS_UNSPECIFIED",
        "FORK_EGRESS_NONE",
    ):
        bad.append("forkConfig.egress must be none (GATEWAY is refused)")
    # validateForkTemplate
    containers = t.get("containers") or []
    if len(containers) != 1:
        bad.append("exactly one container")
    for c in containers:
        if not _SHORT_NAME.match(c.get("name", "")):
            bad.append("container name is not a short name")
        if not re.search(r"@sha256:[0-9a-f]{64}$", c.get("image", "")):
            bad.append("container image must be pinned by digest")
        if not (c.get("wakeupProbe") or {}).get("httpGet"):
            bad.append("a wakeup probe with httpGet")
        if not 1 <= (c.get("wakeupProbe") or {}).get("timeoutSeconds", 0) <= 3600:
            bad.append("wakeupProbe.timeoutSeconds must be 1 to 3600")
        if ((c.get("securityContext") or {}).get("capabilities") or {}).get("add"):
            bad.append("no capabilities to add")
        for env in c.get("env") or []:
            if any(m in env.get("name", "").upper() for m in _SECRET_MARKERS):
                bad.append(f"env {env['name']} is named like a secret")
    if t.get("volumes"):
        bad.append("no volumes")
    golden = ((t.get("snapshotConfig") or {}).get("goldenSnapshotConfig") or {}).get(
        "mode"
    )
    if golden != "GOLDEN_SNAPSHOT_MODE_DISABLED":
        bad.append(
            "snapshotConfig.goldenSnapshotConfig.mode must be GOLDEN_SNAPSHOT_MODE_DISABLED"
        )
    # ActorTemplate: template resources need both limits, the name is a short
    # name, and the golden egress policy needs a preview gate the gate does not use.
    limits = (t.get("resources") or {}).get("limits") or {}
    if not limits.get("cpuMillicores") or not limits.get("memoryBytes"):
        bad.append("resources.limits needs cpu and memory")
    if not _SHORT_NAME.match((t.get("metadata") or {}).get("name", "")):
        bad.append("template name is not a short name")
    if "goldenEgressPolicy" in t:
        bad.append("goldenEgressPolicy is forbidden without its preview gate")
    return bad


def test_template_satisfies_every_fork_validation_rule() -> None:
    assert fork_rule_violations(yaml.safe_load(_TEMPLATE)) == []


def test_the_rule_check_catches_each_violation() -> None:
    import copy

    good = yaml.safe_load(_TEMPLATE)

    def mutated(edit) -> dict:
        t = copy.deepcopy(good)
        edit(t)
        return t

    edits = {
        "golden snapshot mode missing": lambda t: t.pop("snapshotConfig"),
        "golden snapshot enabled": lambda t: t["snapshotConfig"][
            "goldenSnapshotConfig"
        ].update(mode="GOLDEN_SNAPSHOT_MODE_ENABLED"),
        "two containers": lambda t: t["containers"].append(
            dict(t["containers"][0], name="b")
        ),
        "no wakeup probe": lambda t: t["containers"][0].pop("wakeupProbe"),
        "capability added": lambda t: t["containers"][0].update(
            securityContext={"capabilities": {"add": ["NET_ADMIN"]}}
        ),
        "secret-named env": lambda t: t["containers"][0].update(
            env=[{"name": "API_KEY", "value": "x"}]
        ),
        "volume": lambda t: t.update(volumes=[{"name": "v"}]),
        "egress gateway": lambda t: t["forkConfig"].update(
            egress="FORK_EGRESS_GATEWAY"
        ),
        "single use off": lambda t: t["forkConfig"].update(singleUse=False),
        "no seed memory": lambda t: t["forkConfig"]["seedResources"]["limits"].pop(
            "memoryBytes"
        ),
        "lifetime too long": lambda t: t["forkConfig"].update(maxLifetimeSeconds=7200),
        "tag, not digest": lambda t: t["containers"][0].update(
            image="ghcr.io/x/engine-pro:1.186.0"
        ),
        "no template cpu": lambda t: t["resources"]["limits"].pop("cpuMillicores"),
    }
    for name, edit in edits.items():
        assert fork_rule_violations(mutated(edit)), name


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
        self.flaky_workers = 0  # the next N "get workers" calls fail
        self.fail_deletes: set[str] = (
            set()
        )  # kinds whose delete fails (not as "not found")

    def out(self, obj: object, code: int = 0):
        return subprocess.CompletedProcess([], code, json.dumps(obj), "")

    def __call__(self, *args: str):
        self.calls.append(args)
        verb, kind = args[0], args[1]
        rest = list(args[2:])
        atespace = rest[rest.index("-a") + 1] if "-a" in rest else None
        if verb == "get" and kind == "workers" and self.flaky_workers > 0:
            self.flaky_workers -= 1
            return subprocess.CompletedProcess([], 1, "", "rpc error: Unavailable")
        if verb == "delete" and kind in self.fail_deletes:
            return subprocess.CompletedProcess([], 1, "", "rpc error: Internal")
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
    mod.TEMPLATE_JSON_FILE = _HERE / "template.json"
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


def test_template_json_is_the_template_yaml() -> None:
    # provision.py compares the stored template to template.json, because the
    # container cannot parse YAML. The two files must be the same template.
    assert json.loads((_HERE / "template.json").read_text()) == yaml.safe_load(
        _TEMPLATE
    )


def test_apply_compares_the_whole_template_when_it_exists(tmp_path: Path) -> None:
    mod, fake = _load(), FakeAte()
    assert _run(mod, fake, tmp_path) == 0
    stored = fake.templates["engine"]
    # The API adds a uid, a status and defaults: the stored template is a
    # superset of the wanted one, and still matches.
    stored["status"] = {"seeds": 1}
    stored["metadata"]["version"] = "4"
    fake.calls.clear()
    assert _run(mod, fake, tmp_path) == 0 and fake.mutations() == []
    # Any one value that differs, with the image unchanged, is refused.
    edits = {
        "resources": lambda s: s["resources"]["limits"].update(memoryBytes=1),
        "seed": lambda s: s["forkConfig"]["seedResources"]["limits"].update(
            cpuMillicores=500
        ),
        "lifetime": lambda s: s["forkConfig"].update(maxLifetimeSeconds=300),
        "port": lambda s: s["containers"][0]["command"].__setitem__(2, "0.0.0.0:8080"),
        "probe": lambda s: s["containers"][0]["wakeupProbe"].update(timeoutSeconds=5),
        "golden": lambda s: s["snapshotConfig"]["goldenSnapshotConfig"].update(
            mode="GOLDEN_SNAPSHOT_MODE_ENABLED"
        ),
        "selector": lambda s: s["workerSelector"]["matchLabels"].update(
            workload="other"
        ),
        "removed key": lambda s: s["forkConfig"].pop("pidsLimit"),
    }
    import copy

    for name, edit in edits.items():
        fake2 = FakeAte()
        fake2.atespaces.add("egress-scan-engine")
        fake2.templates["engine"] = copy.deepcopy(stored)
        edit(fake2.templates["engine"])
        assert _run(mod, fake2, tmp_path) == 1, name
        assert not [c for c in fake2.mutations() if c[1] == "actor-template"], name


def test_template_diff_rules() -> None:
    mod = _load()
    assert mod.template_diff({"a": "5", "b": 1}, {"a": 5}) == []  # int64 as string
    assert mod.template_diff({"a": [1, 2]}, {"a": [1]}) == [".a"]  # list length
    assert mod.template_diff({"a": {"b": 1}}, {"a": {"b": 2}}) == [".a.b"]
    assert mod.template_diff({}, {"a": 1}) == [".a"]
    assert mod.template_diff({"a": 1}, {"a": {"b": 1}}) == [".a"]


def test_wait_for_tolerates_failed_calls_and_reports_the_last_error() -> None:
    mod = _load()
    calls = {"n": 0}

    def flaky() -> bool:
        calls["n"] += 1
        if calls["n"] < 4:
            raise mod.Failed("get workers: exit 1")
        return True

    mod.wait_for("x", 5, flaky)
    assert calls["n"] == 4
    try:
        mod.wait_for("never", 0.05, lambda: (_ for _ in ()).throw(mod.Failed("boom")))
    except mod.Failed as err:
        assert "last error: boom" in str(err)
    else:
        raise AssertionError("a wait with only failures must end at the limit")


def test_apply_rides_out_api_errors_during_the_seed_wait(tmp_path: Path) -> None:
    mod, fake = _load(), FakeAte()
    fake.flaky_workers = 5
    assert _run(mod, fake, tmp_path) == 0
    assert list(fake.templates) == ["engine"]


def test_names_are_fixed_in_code_not_read_from_the_environment() -> None:
    src = (_HERE / "provision.py").read_text()
    for name in ("ATESPACE", "EDITOR_MEMBER", "TEMPLATE"):
        assert f'environ.get("{name}"' not in src, name
    os.environ.update(ATESPACE="x", EDITOR_MEMBER="user:evil", TEMPLATE="evil")
    try:
        mod = _load()
        assert (mod.ATESPACE, mod.EDITOR, mod.TEMPLATE) == (
            "egress-scan-engine",
            _EDITOR,
            "engine",
        )
    finally:
        for name in ("ATESPACE", "EDITOR_MEMBER", "TEMPLATE"):
            os.environ.pop(name)


def test_the_atespace_check_is_anchored() -> None:
    mod = _load()
    for other in ("egress-scan-engine-other", "xegress-scan-engine"):
        assert mod.template_checks(
            _TEMPLATE.replace("atespace: egress-scan-engine", f"atespace: {other}")
        )
    assert mod.template_checks(_TEMPLATE.replace("  atespace:", "  # atespace:", 1))


def test_apply_makes_no_call_for_a_bad_template(tmp_path: Path) -> None:
    mod, fake = _load(), FakeAte()
    bad = tmp_path / "template.yaml"
    bad.write_text(_TEMPLATE.replace("0.0.0.0:80", "0.0.0.0:8080"))
    mod.ate = fake
    mod.MODE = "apply"
    mod.TEMPLATE_FILE = bad
    mod.TEMPLATE_JSON_FILE = _HERE / "template.json"
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


def _provisioned(mod, tmp_path: Path):
    fake = FakeAte()
    assert _run(mod, fake, tmp_path) == 0
    fake.actors = [
        {
            "metadata": {"name": "eg-engine-gate-0-1-1"},
            "status": {
                "state": "ACTOR_STATE_RUNNING",
                "fork": {"role": "FORK_ROLE_FORK"},
            },
        },
    ]
    fake.calls.clear()
    return fake


def test_teardown_fails_when_a_delete_fails(tmp_path: Path) -> None:
    for kind in ("actor", "actor-template", "access-policy", "atespace"):
        mod = _load()
        fake = _provisioned(mod, tmp_path)
        fake.fail_deletes = {kind}
        assert _run_teardown(mod, fake, tmp_path) == 1, kind
    # A failed actor or template delete stops before the policy and the atespace,
    # which the API would refuse to delete anyway.
    mod = _load()
    fake = _provisioned(mod, tmp_path)
    fake.fail_deletes = {"actor-template"}
    _run_teardown(mod, fake, tmp_path)
    assert ("delete", "atespace") not in [c[:2] for c in fake.calls]
    assert fake.atespaces == {"egress-scan-engine"}


def test_teardown_treats_an_object_that_is_gone_as_done(tmp_path: Path) -> None:
    mod = _load()
    fake = _provisioned(mod, tmp_path)
    plain = fake.__call__

    def gone(*args: str):
        if args[0] == "delete" and args[1] == "actor":
            return subprocess.CompletedProcess([], 1, "", 'actor "x" not found')
        return plain(*args)

    assert _run_teardown(mod, gone, tmp_path) == 0


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
    assert set(cm["data"]) == {"provision.py", "template.yaml", "template.json"}
    assert cm["data"]["template.yaml"] == _TEMPLATE
    assert cm["data"]["template.json"] == (_HERE / "template.json").read_text()
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
