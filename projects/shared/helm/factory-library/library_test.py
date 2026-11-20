# invariant: LIB-1
# invariant: LIB-2
# invariant: LIB-3
# invariant: LIB-4
# invariant: LIB-5
# invariant: LIB-9
# invariant: LIB-7
"""Render the public API, including refusals and offline-safe secret generation."""

import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request

import pytest
import yaml

HERE = Path(__file__).resolve().parent
DIGEST = "sha256:" + "0" * 64
IMAGE = {"repository": "ghcr.io/jomcgi/example", "digest": DIGEST}
RESOURCES = {
    "requests": {"cpu": "10m", "memory": "32Mi"},
    "limits": {"memory": "128Mi"},
}


@pytest.fixture
def chart(tmp_path):
    (tmp_path / "Chart.yaml").write_text(
        "apiVersion: v2\nname: consumer\nversion: 0.1.0\n"
    )
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates/all.yaml").write_text(
        '{{ include "factory-library.all" . }}\n'
    )
    shutil.copytree(HERE / "chart", tmp_path / "charts/factory-library")
    return tmp_path


def values():
    return {
        "image": copy.deepcopy(IMAGE),
        "workloads": {"app": {"resources": copy.deepcopy(RESOURCES)}},
    }


def render(chart, config):
    path = chart / "values.yaml"
    path.write_text(yaml.safe_dump(config))
    return subprocess.run(
        [os.environ["HELM_BIN"], "template", "consumer", str(chart), "-n", "test"],
        capture_output=True,
        text=True,
        check=False,
    )


def objects(result):
    assert result.returncode == 0, result.stderr
    return {
        f"{doc['kind']}/{doc['metadata']['name']}": doc
        for doc in yaml.safe_load_all(result.stdout)
        if doc
    }


def golden(name, actual):
    assert actual == yaml.safe_load((HERE / "goldens" / f"{name}.yaml").read_text())


def test_defaults(chart):
    config = values()
    config["workloads"]["batch"] = {
        "kind": "Job",
        "resources": copy.deepcopy(RESOURCES),
    }
    config["workloads"]["tick"] = {
        "kind": "CronJob",
        "schedule": "0 * * * *",
        "image": {"repository": "registry.example/public", "digest": DIGEST},
        "resources": copy.deepcopy(RESOURCES),
    }
    golden("defaults", objects(render(chart, config)))


def test_declared_paths_service_api_and_peers(chart):
    config = values()
    app = config["workloads"]["app"]
    app.update(
        kubernetesApi=True,
        writablePaths=[{"name": "tmp", "mountPath": "/tmp", "sizeLimit": "16Mi"}],
        service={"ports": [{"name": "http", "port": 8080, "targetPort": "http"}]},
        ports=[{"name": "http", "containerPort": 8080}],
        network={
            "ingress": [{"peer": "edge", "port": 8080}],
            "egress": [
                {"peer": "garage", "port": 3900},
                {"peer": "itsaplan-api", "port": 3000},
                {"peer": "kagent-controller", "port": 8083},
                {"peer": "kube-apiserver", "port": 6443},
                {"peer": "github", "fqdn": "api.github.com", "port": 443},
            ],
        },
    )
    golden("declared", objects(render(chart, config)))


def test_exceptions(chart):
    config = values()
    config["exceptions"] = [
        {
            "workload": "app",
            "default": "runAsUser",
            "value": 1000,
            "reason": "Image UID",
        },
        {
            "workload": "app",
            "default": "runAsGroup",
            "value": 1000,
            "reason": "Image GID",
        },
        {
            "workload": "app",
            "default": "readOnlyRootFilesystem",
            "reason": "Legacy writer",
        },
        {"workload": "app", "default": "network.world", "reason": "External relay"},
        {"workload": "app", "default": "network.0.0.0.0/0", "reason": "IPv4 relay"},
        {"workload": "app", "default": "network.::/0", "reason": "IPv6 relay"},
        {
            "workload": "app",
            "default": "network.fqdn:*.example.com",
            "reason": "Tenant hosts",
        },
    ]
    config["workloads"]["app"]["network"] = {
        "egress": [
            {"peer": "world", "port": 443},
            {"peer": "0.0.0.0/0", "port": 443},
            {"peer": "::/0", "port": 443},
            {"peer": "tenants", "fqdn": "*.example.com", "port": 443},
        ]
    }
    docs = objects(render(chart, config))
    # The complete exception record is visible on both the workload and policy.
    for key in ("Deployment/app", "CiliumNetworkPolicy/app"):
        doc = docs[key]
        assert (
            json.loads(doc["metadata"]["annotations"]["factory.jomcgi.dev/exceptions"])
            == config["exceptions"]
        )
    golden("exceptions", docs)


# Error text is stable even when Helm's call stack or source line changes.
@pytest.mark.parametrize(
    "case", yaml.safe_load((HERE / "goldens/refusals.yaml").read_text())
)
def test_refusals(chart, case):
    config = values()
    for path, value in case["set"].items():
        node = config
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    result = render(chart, config)
    assert result.returncode != 0
    assert "factory-library: " + case["error"] in result.stderr


def generated_config():
    config = values()
    config["secrets"] = {
        "pull": {
            "workload": "app",
            "onePassword": {
                "itemPath": "vaults/factory/items/pull",
                "type": "kubernetes.io/dockerconfigjson",
            },
        },
        "token": {
            "workload": "app",
            "generated": {
                "keys": {"token": 32},
                "image": IMAGE,
                "resources": RESOURCES,
            },
        },
    }
    return config


def test_secrets_are_stable_offline(chart):
    first = render(chart, generated_config())
    second = render(chart, generated_config())
    assert first.stdout == second.stdout
    docs = objects(first)
    container = docs["Job/token-generate"]["spec"]["template"]["spec"]["containers"][0]
    script = container["command"]
    assert script[:2] == ["python3", "-c"]
    compile(script[2], "secret-generator", "exec")
    # Keep the executable script tested below, and the whole manifest golden.
    script[2] = "<secret-generator>"
    golden("secrets", docs)


@pytest.mark.parametrize(
    "existing",
    [{}, {"unrelated": "a2VlcA=="}, {"token": "ZXhpc3Rpbmc=", "unrelated": "a2VlcA=="}],
)
@pytest.mark.parametrize("race", [False, True])
def test_generator_preserves_keys_and_retries_conflicts(
    chart, monkeypatch, existing, race
):
    docs = objects(render(chart, generated_config()))
    job = docs["Job/token-generate"]["spec"]["template"]["spec"]
    script = job["containers"][0]["command"][2]
    for variable in job["containers"][0]["env"]:
        monkeypatch.setenv(variable["name"], variable["value"])
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setattr(Path, "read_text", lambda self: "fixture-token")
    monkeypatch.setattr("ssl.create_default_context", lambda **kwargs: None)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    state = {"metadata": {"resourceVersion": "1"}, "data": copy.deepcopy(existing)}
    writes = []

    def urlopen(request, **kwargs):
        assert request.full_url == (
            "https://10.0.0.1:443/api/v1/namespaces/test/secrets/token"
        )
        if request.get_method() == "PATCH":
            patch = json.loads(request.data)
            assert patch[0] == {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": "1",
            }
            if race:
                state["data"]["token"] = "d2lubmVy"
                state["metadata"]["resourceVersion"] = "2"
                raise urllib.error.HTTPError(
                    request.full_url, 422, "conflict", {}, None
                )
            writes.append(patch)
            state["data"] = patch[1]["value"]
        return io.BytesIO(json.dumps(state).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    exec(compile(script, "secret-generator", "exec"), {})
    for key, value in existing.items():
        assert state["data"][key] == value
    if "token" in existing:
        assert state["data"] == existing
        assert not writes
    elif race:
        assert state["data"]["token"] == "d2lubmVy"
        assert not writes
    else:
        assert len(base64.b64decode(state["data"]["token"])) >= 32
        assert len(writes) == 1


def test_exception_does_not_leak_to_another_workload(chart):
    config = values()
    config["workloads"]["other"] = copy.deepcopy(config["workloads"]["app"])
    config["workloads"]["other"]["network"] = {
        "egress": [{"peer": "world", "port": 443}]
    }
    config["exceptions"] = [
        {"workload": "app", "default": "network.world", "reason": "App only"}
    ]
    result = render(chart, config)
    assert result.returncode != 0
    assert "peer world requires exception network.world" in result.stderr


@pytest.mark.parametrize("kind", ["deployment", "job", "cronjob"])
def test_individual_workload_entry_points(chart, kind):
    (chart / "templates/all.yaml").write_text(
        '{{ include "factory-library.' + kind + '" '
        '(dict "root" . "name" "app" "workload" .Values.workloads.app) }}'
    )
    config = values()
    config["workloads"]["app"].update(replicas=0, backoffLimit=0, schedule="0 * * * *")
    docs = objects(render(chart, config))
    key = {"deployment": "Deployment", "job": "Job", "cronjob": "CronJob"}[kind]
    spec = docs[f"{key}/app"]["spec"]
    if kind == "deployment":
        assert spec["replicas"] == 0
        assert "restartPolicy" not in spec["template"]["spec"]
    else:
        if kind == "cronjob":
            spec = spec["jobTemplate"]["spec"]
        assert spec["backoffLimit"] == 0
        assert spec["template"]["spec"]["restartPolicy"] == "Never"


def test_single_wildcard_requires_and_records_exact_exception(chart):
    config = values()
    config["exceptions"] = [
        {"workload": "app", "default": "network.fqdn:*", "reason": "Legacy proxy"}
    ]
    config["workloads"]["app"]["network"] = {
        "egress": [{"peer": "proxy", "fqdn": "*", "port": 443}]
    }
    docs = objects(render(chart, config))
    assert docs["CiliumNetworkPolicy/app"]["spec"]["egress"][0]["toFQDNs"] == [
        {"matchPattern": "*"}
    ]


@pytest.mark.parametrize("hook", ["Sync", "PostSync"])
def test_job_hook_runs_every_sync(chart, hook):
    config = values()
    config["workloads"]["app"].update({"kind": "Job", "hook": hook})
    job = objects(render(chart, config))["Job/app"]
    assert job["metadata"]["annotations"] == {
        "argocd.argoproj.io/hook": hook,
        "argocd.argoproj.io/hook-delete-policy": "BeforeHookCreation,HookSucceeded",
    }


@pytest.mark.parametrize("wave", [-1, 0, 1])
def test_job_hooks_can_wait_for_an_earlier_wave(chart, wave):
    config = values()
    config["workloads"]["app"].update(
        {"kind": "Job", "hook": "PostSync", "hookWave": wave}
    )
    job = objects(render(chart, config))["Job/app"]
    assert job["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == str(wave)


@pytest.mark.parametrize("wave", [0.5, "first"])
def test_hook_wave_requires_an_integer(chart, wave):
    config = values()
    config["workloads"]["app"].update(
        {"kind": "Job", "hook": "PostSync", "hookWave": wave}
    )
    result = render(chart, config)
    assert result.returncode != 0
    assert "hookWave must be an integer" in result.stderr


def test_hook_wave_requires_a_hook(chart):
    config = values()
    config["workloads"]["app"].update({"kind": "Job", "hookWave": 1})
    result = render(chart, config)
    assert result.returncode != 0
    assert "hookWave requires a Job hook" in result.stderr


def test_job_without_hook_is_a_plain_resource(chart):
    config = values()
    config["workloads"]["app"]["kind"] = "Job"
    job = objects(render(chart, config))["Job/app"]
    assert "annotations" not in job["metadata"]


def bucket_config():
    config = values()
    config["image"]["repository"] = "registry.example/public"
    config["buckets"] = [
        {
            "name": "factory-public",
            "access": "write",
            "public": {
                "r2Bucket": "factory-public",
                "itemPath": "vaults/k8s-homelab/items/factory-r2-public-mirror",
            },
        }
    ]
    return config


def mirror_container(docs, name="consumer-factory-public-mirror"):
    return docs[f"CronJob/{name}"]["spec"]["jobTemplate"]["spec"]["template"]["spec"][
        "containers"
    ][0]


def test_public_bucket_defaults(chart):
    config = bucket_config()
    first = render(chart, config)
    assert first.stdout == render(chart, config).stdout
    docs = objects(first)
    container = mirror_container(docs)
    # The executable shell is exercised below; the golden pins the resources.
    container["command"][2] = "<mirror-script>"
    golden("buckets", docs)


def test_bucket_discovery_cannot_list_credentials(chart):
    docs = objects(render(chart, bucket_config()))
    claims = {
        d["metadata"]["name"]: d for d in docs.values() if d["kind"] == "ConfigMap"
    }
    secrets = {d["metadata"]["name"] for d in docs.values() if d["kind"] == "Secret"}
    assert len(claims) == 2
    assert {claim["data"]["secretName"] for claim in claims.values()} == secrets
    for claim in claims.values():
        assert claim["metadata"]["namespace"] == "garage"
        assert claim["data"]["namespace"] == "test"
        assert claim["data"]["bucket"] == "factory-public"
        expected = hashlib.sha256(
            f"test/{claim['data']['secretName']}".encode()
        ).hexdigest()
        assert claim["metadata"]["name"] == "garage-claim-" + expected
        assert claim["data"]["access"] == (
            "read" if claim["data"]["secretName"].endswith("-mirror-s3") else "write"
        )
    for doc in docs.values():
        if doc["kind"] != "Role":
            continue
        for rule in doc["rules"]:
            assert rule["resources"] == ["secrets"]
            assert rule["verbs"] == ["get", "update"]
            assert set(rule["resourceNames"]) <= secrets


@pytest.mark.parametrize("suspend", [False, True])
def test_public_bucket_suspension(chart, suspend):
    config = bucket_config()
    config["buckets"][0]["public"]["suspend"] = suspend
    job = objects(render(chart, config))["CronJob/consumer-factory-public-mirror"]
    assert job["spec"]["suspend"] is suspend
    assert job["spec"]["schedule"] == "* * * * *"


def readme_deadline():
    """Return the one mirror deadline that the README's writer rule names."""
    text = (HERE / "README.md").read_text()
    found = re.findall(r"activeDeadlineSeconds: (\d+)", text)
    assert len(found) == 1, found
    return int(found[0])


def test_every_mirror_job_stops_before_the_writer_retention(chart):
    deadline = readme_deadline()
    config = sources_config()
    config["workloads"]["app"].update(kind="CronJob", schedule="0 * * * *")
    docs = objects(render(chart, config))
    jobs = {
        doc["metadata"]["name"]: doc["spec"]["jobTemplate"]["spec"]
        for doc in docs.values()
        if doc["kind"] == "CronJob"
    }
    mirrors = {
        name: job
        for name, job in jobs.items()
        if any(
            var["name"] == "R2_BUCKET"
            for var in job["template"]["spec"]["containers"][0].get("env", [])
        )
    }
    assert sorted(mirrors) == [
        "consumer-factory-public-mirror",
        "consumer-hikes-mirror",
        "consumer-stars-forecast-mirror",
    ]
    for name, job in mirrors.items():
        # The deadline bounds the Job, so it also bounds the retry pod.
        assert isinstance(job["activeDeadlineSeconds"], int), name
        assert 0 < job["activeDeadlineSeconds"] <= deadline, name
        assert job["backoffLimit"] == 1, name
        # The writer rule adds the default 30-second termination grace.
        assert "terminationGracePeriodSeconds" not in job["template"]["spec"], name
    assert "activeDeadlineSeconds" not in jobs["app"]


def test_private_bucket_and_custom_schedule(chart):
    config = bucket_config()
    config["buckets"][0].pop("public")
    docs = objects(render(chart, config))
    assert not any(key.startswith(("CronJob/", "OnePasswordItem/")) for key in docs)
    assert docs["CiliumNetworkPolicy/app"]["spec"]["egress"][0]["toPorts"] == [
        {"ports": [{"port": "3900", "protocol": "TCP"}]}
    ]
    assert (
        docs["Secret/consumer-factory-public-s3"]["metadata"]["labels"][
            "factory.jomcgi.dev/garage-access"
        ]
        == "write"
    )
    config = bucket_config()
    config["buckets"][0]["public"]["schedule"] = "*/5 * * * *"
    docs = objects(render(chart, config))
    assert docs["CronJob/consumer-factory-public-mirror"]["spec"]["schedule"] == (
        "*/5 * * * *"
    )


# The run without a failure: two Garage listings around the manifest capture,
# the payload copy, the R2 listing, then the manifest.
ORDER = ["lsf", "lsf", "cat", "lsf", "copy", "lsf", "copyto"]


@pytest.mark.parametrize(
    "manifest,fail,expected",
    [
        ("manifest.json\n", "", ORDER),
        ("", "", ["lsf", "lsf", "lsf", "copy", "lsf", "delete"]),
        ("manifest.json\n", "lsf", ["lsf"]),
        ("manifest.json\n", "cat", ORDER[:3]),
        ("manifest.json\n", "copy", ORDER[:5]),
        ("manifest.json\n", "copyto", ORDER),
        ("", "delete", ["lsf", "lsf", "lsf", "copy", "lsf", "delete"]),
    ],
)
@pytest.mark.parametrize("name", ["manifest.json", "latest.json"])
@pytest.mark.parametrize("source", [False, True])
def test_mirror_orders_writes_and_propagates_failures(
    chart, tmp_path, manifest, fail, expected, name, source
):
    config = bucket_config()
    garage, dest = "GARAGE:factory-public", "R2:factory-public"
    job = "consumer-factory-public-mirror"
    if source:
        # A source mirror writes and deletes only under its prefix.
        config["buckets"][0]["public"]["sources"] = [
            {"bucket": "hikes", "r2Prefix": "hikes", "manifestName": name}
        ]
        garage, dest, job = (
            "GARAGE:hikes",
            "R2:factory-public/hikes",
            "consumer-hikes-mirror",
        )
    elif name == "latest.json":
        config["buckets"][0]["public"]["manifestName"] = name
    docs = objects(render(chart, config))
    container = mirror_container(docs, job)
    env = {
        **os.environ,
        **{var["name"]: var["value"] for var in container["env"] if "value" in var},
    }
    env.update(
        RCLONE_CONFIG_R2_ENDPOINT="https://" + env["R2_HOST"],
        FAKE_MANIFEST=manifest.replace("manifest.json", name),
        FAKE_FAIL=fail,
        FAKE_LOG=str(tmp_path / "calls.jsonl"),
    )
    binary = tmp_path / "rclone"
    binary.write_text(
        "#!" + shutil.which("python3") + "\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['FAKE_LOG'], 'a') as out:\n"
        "    out.write(json.dumps(args) + '\\n')\n"
        "command = args[3]\n"
        "if command == os.environ['FAKE_FAIL']: sys.exit(1)\n"
        "if command == 'cat': sys.stdout.buffer.write(b'generation-one\\n')\n"
        "if command == 'copyto':\n"
        "    assert open(args[4], 'rb').read() == b'generation-one\\n'\n"
        "if command == 'lsf' and '--max-depth' in args:\n"
        "    print(os.environ['FAKE_MANIFEST'], end='')\n"
    )
    binary.chmod(0o755)
    env["PATH"] = str(tmp_path) + os.pathsep + env["PATH"]
    result = subprocess.run(container["command"], env=env, capture_output=True)
    assert (result.returncode != 0) == bool(fail)
    calls = [
        json.loads(line) for line in Path(env["FAKE_LOG"]).read_text().splitlines()
    ]
    # Garage first, then payloads with no delete, then the manifest.
    assert [call[3] for call in calls] == expected
    assert all(
        call[:3] == ["--config", "/dev/null", "--s3-no-check-bucket"] for call in calls
    )
    filters = ["--exclude", f"/{name}"]
    listings = [call[4:] for call in calls if call[3] == "lsf"]
    for listing, remote in zip(listings, [garage, garage, garage, dest]):
        if "-R" in listing:
            assert listing == [*filters, "-R", "--files-only", remote]
        else:
            assert listing == [
                remote,
                "--files-only",
                "--max-depth",
                "1",
                "--include",
                f"/{name}",
            ]
    # Both Garage listings come before and after the capture.
    assert ["-R" in listing for listing in listings] == (
        [True, False, True, True][: len(listings)]
    )
    for call in calls:
        if call[3] == "cat":
            assert call[4:] == [f"{garage}/{name}"]
        if call[3] == "copy":
            assert call[4:] == [
                *filters,
                garage,
                dest,
                "--header-upload",
                "Cache-Control: public, max-age=31536000, immutable",
            ]
        if call[3] == "copyto":
            # The run publishes the manifest it captured, not Garage's
            # current one.
            assert call[4].startswith("/")
            assert call[5:] == [
                f"{dest}/{name}",
                "--ignore-times",
                "--header-upload",
                "Cache-Control: public, max-age=60",
            ]
        if call[3] == "delete":
            assert call[4:] == [dest, "--include", f"/{name}"]


def fake_rclone(tmp_path):
    """Install the directory-backed rclone stand-in; return its PATH entry."""
    binary = tmp_path / "bin" / "rclone"
    binary.parent.mkdir(exist_ok=True)
    binary.write_text(
        "#!" + shutil.which("python3") + "\n" + (HERE / "fake_rclone.py").read_text()
    )
    binary.chmod(0o755)
    return str(binary.parent)


def write_generation(bucket, generation, manifest):
    """Write two digest-named payloads, then the manifest that names them."""
    keys = []
    bucket.mkdir(parents=True, exist_ok=True)
    for part in ("a", "b"):
        body = json.dumps({"generation": generation, "part": part}).encode()
        key = f"{part}-{hashlib.sha256(body).hexdigest()}.json"
        (bucket / key).write_bytes(body)
        keys.append(key)
    (bucket / manifest).write_text(json.dumps(keys))
    return set(keys)


def public_view(prefix, manifest):
    """The objects a reader resolves through the public manifest.

    Every key the manifest names must exist with the bytes its digest names.
    """
    if not (prefix / manifest).exists():
        return set()
    keys = set(json.loads((prefix / manifest).read_text()))
    for key in keys:
        digest = key.rsplit("-", 1)[1].removesuffix(".json")
        assert (prefix / key).exists(), f"{manifest} names missing {key}"
        assert hashlib.sha256((prefix / key).read_bytes()).hexdigest() == digest
    return keys


CHECK = b"a listed payload left Garage before the copy"


class Mirror:
    """One mirror job, its Garage bucket and its R2 prefix, as directories."""

    def __init__(self, chart, tmp_path, name, source, repair_cache_key=None):
        config = bucket_config()
        entry = {"bucket": "hikes", "r2Prefix": "hikes", "manifestName": name}
        if repair_cache_key is not None:
            entry["repairCacheKey"] = repair_cache_key
        config["buckets"][0]["public"]["sources"] = [entry]
        bucket, self.prefix, job = (
            "factory-public",
            "",
            "consumer-factory-public-mirror",
        )
        if source:
            bucket, self.prefix, job = "hikes", "hikes", "consumer-hikes-mirror"
        else:
            config["buckets"][0]["public"]["manifestName"] = name
        self.container = mirror_container(objects(render(chart, config)), job)
        self.tmp, self.name = tmp_path, name
        self.garage, self.r2 = tmp_path / "garage", tmp_path / "r2"
        self.bucket = self.garage / bucket
        self.bucket.mkdir(parents=True)
        (self.r2 / "factory-public").mkdir(parents=True)
        # An object under another mirror's prefix. The root mirror keeps it.
        self.other = (
            self.r2 / "factory-public" / ("stars" if source else "hikes") / "keep.json"
        )
        self.other.parent.mkdir(parents=True)
        self.other.write_text("{}")
        self.path = fake_rclone(tmp_path)

    def public(self, r2=None):
        return (r2 or self.r2) / "factory-public" / self.prefix

    def view(self, r2=None):
        return public_view(self.public(r2), self.name)

    def stored(self):
        """Every object under the prefix, except other mirrors' objects."""
        return {
            p.relative_to(self.public()).as_posix()
            for p in self.public().rglob("*")
            if p.is_file() and not p.is_relative_to(self.other.parent)
        }

    def run(self, garage=None, r2=None, **fake):
        state = tempfile.mkdtemp(dir=self.tmp)
        env = {
            **os.environ,
            **{
                var["name"]: var["value"]
                for var in self.container["env"]
                if "value" in var
            },
            "FAKE_REMOTES": json.dumps(
                {"GARAGE": str(garage or self.garage), "R2": str(r2 or self.r2)}
            ),
            "FAKE_STATE": state,
            "PATH": self.path + os.pathsep + os.environ["PATH"],
            **{f"FAKE_{key.upper()}": str(value) for key, value in fake.items()},
        }
        env["RCLONE_CONFIG_R2_ENDPOINT"] = "https://" + env["R2_HOST"]
        result = subprocess.run(self.container["command"], env=env, capture_output=True)
        result.state = Path(state)
        return result

    def copy(self, directory):
        """A fresh copy of a remote root for one run."""
        target = Path(tempfile.mkdtemp(dir=self.tmp)) / directory.name
        shutil.copytree(directory, target)
        return target


def test_mirror_repairs_cache_metadata_without_changing_payload(chart, tmp_path):
    body = b'{"history":[]}'
    key = f"stars/history-map-{hashlib.sha256(body).hexdigest()}.json"
    mirror = Mirror(chart, tmp_path, "latest.json", True, repair_cache_key=key)
    for bucket in (mirror.bucket, mirror.public()):
        target = bucket / key
        target.parent.mkdir(parents=True)
        target.write_bytes(body)
    (mirror.bucket / mirror.name).write_text(json.dumps({"files": {"historyMap": key}}))

    repaired = mirror.run()
    assert repaired.returncode == 0, repaired.stderr
    headers = repaired.state / "headers.json"
    assert json.loads(headers.read_text()) == {
        str(mirror.public() / key): [
            "Cache-Control: public, max-age=31536000, immutable"
        ],
        str(mirror.public() / mirror.name): ["Cache-Control: public, max-age=60"],
    }
    assert (mirror.public() / key).read_bytes() == body

    repeated = mirror.run(prior_headers=headers)
    assert repeated.returncode == 0, repeated.stderr
    assert str(mirror.public() / key) not in json.loads(
        (repeated.state / "headers.json").read_text()
    )
    assert mirror.other.read_text() == "{}"


@pytest.mark.parametrize("name", ["manifest.json", "latest.json"])
@pytest.mark.parametrize("source", [False, True])
def test_interrupted_mirror_keeps_the_public_manifest_complete(
    chart, tmp_path, name, source
):
    mirror = Mirror(chart, tmp_path, name, source)
    first = write_generation(mirror.bucket, 1, name)
    assert mirror.run().returncode == 0
    assert mirror.view() == first
    # A long outage: the writer publishes a second generation and removes the
    # payloads that only the first one named, which R2 still serves.
    second = write_generation(mirror.bucket, 2, name)
    for key in first:
        (mirror.bucket / key).unlink()
    # Stop a run before each call and between each object write or delete.
    budget = 0
    while True:
        attempt = mirror.copy(mirror.r2)
        result = mirror.run(r2=attempt, budget=budget)
        assert mirror.view(attempt) in (first, second), budget
        if result.returncode == 0:
            break
        assert b"interrupted" in result.stderr, result.stderr
        budget += 1
    # Four listings and a cat, the copy and its two uploads, the manifest and
    # its upload, then the delete and its two deletes.
    assert budget == 13
    # Recovery: the next complete run catches up and removes the old payloads.
    assert mirror.run().returncode == 0
    assert mirror.view() == second
    assert mirror.stored() == second | {name}
    assert mirror.other.read_text() == "{}"


@pytest.mark.parametrize("behind", [True, False])
@pytest.mark.parametrize(
    "name,source", [("manifest.json", False), ("latest.json", True)]
)
def test_writer_during_the_run_keeps_the_public_manifest_complete(
    chart, tmp_path, name, source, behind
):
    """The writer publishes a new generation at any step of a mirror run.

    The writer puts its payloads, then its manifest, then deletes every payload
    that the new manifest does not name. The fake copies R2 after each object
    write or delete, so the checks below see the state at every stop point.
    """
    mirror = Mirror(chart, tmp_path, name, source)
    first = write_generation(mirror.bucket, 1, name)
    assert mirror.run().returncode == 0
    second = write_generation(mirror.bucket, 2, name)
    for key in first:
        (mirror.bucket / key).unlink()
    if not behind:
        # R2 already serves the second generation.
        assert mirror.run().returncode == 0
    before = mirror.view()
    assert before == (first if behind else second)
    staged = tmp_path / "staged"
    third = write_generation(staged, 3, name)
    outcomes = set()
    at = 0
    while True:
        garage, r2 = mirror.copy(mirror.garage), mirror.copy(mirror.r2)
        snapshots = Path(tempfile.mkdtemp(dir=tmp_path))
        advance = {
            "at": at,
            "from": str(staged),
            "to": str(garage / mirror.bucket.name),
            "manifest": name,
        }
        result = mirror.run(
            garage, r2, advance=json.dumps(advance), snapshots=snapshots
        )
        if not (result.state / "advanced").exists():
            # The run ended before step `at`.
            assert result.returncode == 0
            break
        # Either the run completes, or it stops before the manifest because
        # the writer removed a listed payload.
        assert result.returncode == 0 or CHECK in result.stderr, result.stderr
        outcomes.add(result.returncode)
        for state in sorted(snapshots.iterdir()):
            assert mirror.view(state) in (before, second, third), (at, state.name)
        assert (r2 / mirror.other.relative_to(mirror.r2)).read_text() == "{}"
        # Recovery: the next run without the writer publishes the third
        # generation and removes every older payload.
        assert mirror.run(garage, r2).returncode == 0
        assert mirror.view(r2) == third
        at += 1
    # The writer ran before every step of the run: 13 steps when R2 lags,
    # 8 when the copy uploads nothing and the delete step has nothing to do.
    assert at == (13 if behind else 8)
    # When R2 lags, a writer that removes the captured generation before the
    # copy stops the run before the manifest.
    assert outcomes == ({0, 1} if behind else {0})


def test_mirror_refuses_endpoint_outside_policy_before_running_rclone(chart):
    container = mirror_container(objects(render(chart, bucket_config())))
    env = {var["name"]: var["value"] for var in container["env"] if "value" in var}
    env["RCLONE_CONFIG_R2_ENDPOINT"] = "https://other.r2.cloudflarestorage.com"
    result = subprocess.run(
        container["command"], env=env, capture_output=True, text=True
    )
    assert result.returncode == 1
    assert result.stderr.strip() == "R2 endpoint does not match the declared egress"


def copy_config():
    config = bucket_config()
    config["buckets"][0]["public"]["copyKeys"] = [
        {"bucket": "factory-health", "key": "health.json"}
    ]
    config["buckets"].append({"name": "factory-health", "access": "write"})
    return config


def test_copied_key_has_its_own_read_claim(chart):
    docs = objects(render(chart, copy_config()))
    claims = {
        d["data"]["secretName"]: (d["data"]["bucket"], d["data"]["access"])
        for d in docs.values()
        if d["kind"] == "ConfigMap"
    }
    # The writer's key covers only its own bucket; the mirror reads it.
    assert claims == {
        "consumer-factory-public-s3": ("factory-public", "write"),
        "consumer-factory-public-mirror-s3": ("factory-public", "read"),
        "consumer-factory-public-mirror-factory-health-s3": ("factory-health", "read"),
        "consumer-factory-health-s3": ("factory-health", "write"),
    }
    env = {
        var["name"]: var.get("value") or var["valueFrom"]["secretKeyRef"]["name"]
        for var in mirror_container(docs)["env"]
        if var["name"].startswith("RCLONE_CONFIG_SRC0_")
    }
    assert env["RCLONE_CONFIG_SRC0_PROVIDER"] == "Other"
    assert env["RCLONE_CONFIG_SRC0_ACCESS_KEY_ID"] == (
        "consumer-factory-public-mirror-factory-health-s3"
    )


@pytest.mark.parametrize("present,last", [("health.json", "copyto"), ("", "delete")])
def test_mirror_copies_only_the_named_key_with_a_short_cache(
    chart, tmp_path, present, last
):
    container = mirror_container(objects(render(chart, copy_config())))
    env = {
        **os.environ,
        **{var["name"]: var["value"] for var in container["env"] if "value" in var},
    }
    env.update(
        RCLONE_CONFIG_R2_ENDPOINT="https://" + env["R2_HOST"],
        FAKE_PRESENT=present,
        FAKE_LOG=str(tmp_path / "calls.jsonl"),
    )
    binary = tmp_path / "rclone"
    binary.write_text(
        "#!" + shutil.which("python3") + "\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['FAKE_LOG'], 'a') as out:\n"
        "    out.write(json.dumps(args) + '\\n')\n"
        "if args[3] == 'lsf' and args[4].startswith('SRC0:'):\n"
        "    print(os.environ['FAKE_PRESENT'])\n"
    )
    binary.chmod(0o755)
    env["PATH"] = str(tmp_path) + os.pathsep + env["PATH"]
    result = subprocess.run(container["command"], env=env, capture_output=True)
    assert result.returncode == 0, result.stderr
    calls = [
        json.loads(line) for line in Path(env["FAKE_LOG"]).read_text().splitlines()
    ]
    # The listings and the payload copy leave the copied key out, so the run
    # neither uploads it with the immutable cache nor deletes it from R2.
    filtered = [call for call in calls if call[3] == "copy" or "-R" in call]
    assert [call[3] for call in filtered] == ["lsf", "lsf", "copy", "lsf"]
    for call in filtered:
        assert call[4:8] == ["--exclude", "/manifest.json", "--exclude", "/health.json"]
    assert calls[-2][3:] == [
        "lsf",
        "SRC0:factory-health",
        "--files-only",
        "--max-depth",
        "1",
        "--include",
        "/health.json",
    ]
    if last == "copyto":
        assert calls[-1][3:] == [
            "copyto",
            "SRC0:factory-health/health.json",
            "R2:factory-public/health.json",
            "--ignore-times",
            "--header-upload",
            "Cache-Control: public, max-age=60",
        ]
    else:
        assert calls[-1][3:] == [
            "delete",
            "R2:factory-public",
            "--include",
            "/health.json",
        ]


@pytest.mark.parametrize(
    "entry",
    [
        {"bucket": "factory-public", "key": "health.json"},
        {"bucket": "elsewhere", "key": "health.json"},
        {"bucket": "factory-health", "key": "manifest.json"},
        {"bucket": "factory-health", "key": "dir/health.json"},
        {"bucket": "factory-health", "key": "health.txt"},
        {"bucket": "factory-health", "key": "health.json", "extra": 1},
        "health.json",
    ],
)
def test_copy_keys_name_another_bucket_and_a_root_json_key(chart, entry):
    config = copy_config()
    config["buckets"][0]["public"]["copyKeys"] = [entry]
    result = render(chart, config)
    assert result.returncode != 0
    assert "copyKeys" in result.stderr


def test_copy_keys_are_unique(chart):
    config = copy_config()
    config["buckets"][0]["public"]["copyKeys"] *= 2
    result = render(chart, config)
    assert result.returncode != 0
    assert "copyKeys" in result.stderr


def sources_config():
    config = copy_config()
    config["buckets"][0]["public"]["sources"] = [
        {
            "bucket": "stars-forecast",
            "r2Prefix": "stars-forecast",
            "manifestName": "latest.json",
            "suspend": True,
        },
        {"bucket": "hikes", "r2Prefix": "hikes", "schedule": "*/5 * * * *"},
    ]
    return config


@pytest.mark.parametrize("active", [None, "root", "source"])
def test_mirror_item_path_is_optional_only_while_all_jobs_are_suspended(chart, active):
    config = sources_config()
    public = config["buckets"][0]["public"]
    del public["itemPath"]
    public["suspend"] = active != "root"
    public["sources"][1]["suspend"] = active != "source"

    result = render(chart, config)
    if active:
        assert result.returncode != 0
        assert "public.itemPath is required" in result.stderr
        return

    docs = objects(result)
    jobs = [doc for doc in docs.values() if doc["kind"] == "CronJob"]
    assert len(jobs) == 3
    for job in jobs:
        assert job["spec"]["suspend"]
        name = job["metadata"]["name"]
        assert f"OnePasswordItem/{name}-r2" not in docs
        env = {var["name"]: var for var in mirror_container(docs, name)["env"]}
        assert env["RCLONE_CONFIG_R2_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"] == {
            "name": f"{name}-r2",
            "key": "access-key-id",
        }


def run_fake_mirror(container, tmp_path, listing=""):
    """Run a mirror script against an rclone that logs each call."""
    env = {
        **os.environ,
        **{var["name"]: var["value"] for var in container["env"] if "value" in var},
    }
    env.update(
        RCLONE_CONFIG_R2_ENDPOINT="https://" + env["R2_HOST"],
        FAKE_LISTING=listing,
        FAKE_LOG=str(tmp_path / "calls.jsonl"),
    )
    binary = tmp_path / "rclone"
    binary.write_text(
        "#!" + shutil.which("python3") + "\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['FAKE_LOG'], 'a') as out:\n"
        "    out.write(json.dumps(args) + '\\n')\n"
        "if args[3] == 'lsf' and '--max-depth' in args:\n"
        "    print(os.environ['FAKE_LISTING'], end='')\n"
    )
    binary.chmod(0o755)
    env["PATH"] = str(tmp_path) + os.pathsep + env["PATH"]
    result = subprocess.run(container["command"], env=env, capture_output=True)
    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in Path(env["FAKE_LOG"]).read_text().splitlines()]


def test_sources_get_read_claims_and_their_own_mirrors(chart):
    docs = objects(render(chart, sources_config()))
    claims = {
        d["data"]["secretName"]: (d["data"]["bucket"], d["data"]["access"])
        for d in docs.values()
        if d["kind"] == "ConfigMap"
    }
    # The release reads each source bucket and writes none of them.
    assert claims == {
        "consumer-factory-public-s3": ("factory-public", "write"),
        "consumer-factory-public-mirror-s3": ("factory-public", "read"),
        "consumer-factory-public-mirror-factory-health-s3": ("factory-health", "read"),
        "consumer-factory-health-s3": ("factory-health", "write"),
        "consumer-stars-forecast-mirror-s3": ("stars-forecast", "read"),
        "consumer-hikes-mirror-s3": ("hikes", "read"),
    }
    items = {
        d["metadata"]["name"]: d["spec"]["itemPath"]
        for d in docs.values()
        if d["kind"] == "OnePasswordItem"
    }
    assert items == {
        f"consumer-{name}-mirror-r2": (
            "vaults/k8s-homelab/items/factory-r2-public-mirror"
        )
        for name in ("factory-public", "stars-forecast", "hikes")
    }
    jobs = {
        d["metadata"]["name"]: d["spec"]
        for d in docs.values()
        if d["kind"] == "CronJob" and d["metadata"]["name"] != "app"
    }
    assert {name: (job["suspend"], job["schedule"]) for name, job in jobs.items()} == {
        "consumer-factory-public-mirror": (False, "* * * * *"),
        "consumer-stars-forecast-mirror": (True, "* * * * *"),
        "consumer-hikes-mirror": (False, "*/5 * * * *"),
    }
    for name in jobs:
        pod = jobs[name]["jobTemplate"]["spec"]["template"]["spec"]
        assert pod["serviceAccountName"] == name
        assert pod["automountServiceAccountToken"] is False
        env = {
            var["name"]: var["valueFrom"]["secretKeyRef"]["name"]
            for var in pod["containers"][0]["env"]
            if "valueFrom" in var
        }
        assert env["RCLONE_CONFIG_R2_ACCESS_KEY_ID"] == f"{name}-r2"
        assert env["RCLONE_CONFIG_GARAGE_ACCESS_KEY_ID"] == f"{name}-s3"
        # Only the mirror chain may use each R2 Secret.
        for suffix in ("", "-jobs", "-cronjobs"):
            policy = docs[f"ValidatingAdmissionPolicy/test-{name}-r2{suffix}"]
            assert f'"{name}"' in policy["spec"]["validations"][0]["expression"]
    hikes = mirror_container(docs, "consumer-hikes-mirror")
    assert "SRC0" not in json.dumps(hikes["env"])
    assert {var["name"]: var.get("value") for var in hikes["env"]}["GARAGE_BUCKET"] == (
        "hikes"
    )


@pytest.mark.parametrize(
    "sources", [[], ["stars-forecast"], ["stars-forecast", "hikes"]]
)
def test_root_sync_excludes_every_source_prefix(chart, tmp_path, sources):
    config = copy_config()
    config["buckets"][0]["public"]["sources"] = [
        {"bucket": f"{name}-data", "r2Prefix": name} for name in sources
    ]
    container = mirror_container(objects(render(chart, config)))
    calls = run_fake_mirror(container, tmp_path)
    excludes = [
        "--exclude",
        "/manifest.json",
        "--exclude",
        "/health.json",
        *[arg for name in sources for arg in ("--exclude", f"/{name}/**")],
    ]
    # Both Garage listings, the copy and the R2 listing leave every source
    # prefix out, so the delete list never names a source object.
    filtered = [call[3:] for call in calls if call[3] == "copy" or "-R" in call]
    assert filtered == [
        ["lsf", *excludes, "-R", "--files-only", "GARAGE:factory-public"],
        ["lsf", *excludes, "-R", "--files-only", "GARAGE:factory-public"],
        [
            "copy",
            *excludes,
            "GARAGE:factory-public",
            "R2:factory-public",
            "--header-upload",
            "Cache-Control: public, max-age=31536000, immutable",
        ],
        ["lsf", *excludes, "-R", "--files-only", "R2:factory-public"],
    ]
    assert not any(call[3] == "sync" or "--delete-excluded" in call for call in calls)


@pytest.mark.parametrize("listing", ["", "latest.json"])
def test_source_writes_and_deletes_only_under_its_prefix(chart, tmp_path, listing):
    docs = objects(render(chart, sources_config()))
    container = mirror_container(docs, "consumer-stars-forecast-mirror")
    calls = run_fake_mirror(container, tmp_path, listing)
    assert [call[3] for call in calls] == (
        ["lsf", "lsf"]
        + (["cat"] if listing else [])
        + ["lsf", "copy", "lsf"]
        + (["copyto"] if listing else ["delete"])
    )
    remotes = [arg for call in calls for arg in call if arg.startswith("R2:")]
    assert remotes
    assert all(
        arg == "R2:factory-public/stars-forecast"
        or arg.startswith("R2:factory-public/stars-forecast/")
        for arg in remotes
    )
    # It reads only its source bucket.
    assert all(
        arg == "GARAGE:stars-forecast" or arg.startswith("GARAGE:stars-forecast/")
        for call in calls
        for arg in call
        if arg.startswith("GARAGE:")
    )


@pytest.mark.parametrize("prefix", ["a", "stars-forecast", "a1-b2"])
def test_source_prefix_accepts_one_path_segment(chart, prefix):
    config = bucket_config()
    config["buckets"][0]["public"]["sources"] = [{"bucket": "data", "r2Prefix": prefix}]
    objects(render(chart, config))


@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "a/b",
        "/a",
        "a/",
        "..",
        "../a",
        "a..b",
        "a.b",
        "Stars",
        "-a",
        "a-",
        "a b",
        "x" * 64,
        "*",
        5,
        True,
        None,
        ["a"],
        {"a": 1},
    ],
)
def test_source_prefix_refuses_anything_but_one_safe_segment(chart, prefix):
    config = bucket_config()
    config["buckets"][0]["public"]["sources"] = [{"bucket": "data", "r2Prefix": prefix}]
    result = render(chart, config)
    assert result.returncode != 0
    assert (
        "factory-library: public.sources r2Prefix must be one path segment of "
        "lowercase letters, digits and -"
    ) in result.stderr


@pytest.mark.parametrize(
    "sources,error",
    [
        ("hikes", "public.sources must be a list"),
        ({"bucket": "hikes"}, "public.sources must be a list"),
        (["hikes"], "public.sources entries must be maps"),
        ([{"r2Prefix": "hikes"}], "public.sources bucket is required"),
        ([{"bucket": "hikes"}], "public.sources r2Prefix is required"),
        (
            [{"bucket": "factory-public", "r2Prefix": "hikes"}],
            "public.sources buckets must be unique Garage bucket names",
        ),
        (
            [{"bucket": "Hikes", "r2Prefix": "hikes"}],
            "public.sources buckets must be unique Garage bucket names",
        ),
        (
            [{"bucket": 5, "r2Prefix": "hikes"}],
            "public.sources buckets must be unique Garage bucket names",
        ),
        (
            [
                {"bucket": "hikes", "r2Prefix": "hikes"},
                {"bucket": "hikes", "r2Prefix": "walks"},
            ],
            "public.sources buckets must be unique Garage bucket names",
        ),
        (
            [
                {"bucket": "hikes", "r2Prefix": "walks"},
                {"bucket": "walks", "r2Prefix": "walks"},
            ],
            "public.sources r2Prefix values must be unique",
        ),
        (
            [{"bucket": "hikes", "r2Prefix": "hikes", "suspend": "yes"}],
            "public.sources suspend must be a boolean",
        ),
        (
            [{"bucket": "hikes", "r2Prefix": "hikes", "manifestName": "index.json"}],
            "public.sources manifestName must be manifest.json or latest.json",
        ),
        (
            [
                {
                    "bucket": "hikes",
                    "r2Prefix": "hikes",
                    "repairCacheKey": "../walks-" + "a" * 64 + ".json",
                }
            ],
            "public.sources repairCacheKey must be a relative, SHA-256-named JSON payload",
        ),
        # A source has no R2 settings of its own: the release supplies them.
        (
            [{"bucket": "hikes", "r2Prefix": "hikes", "itemPath": "vaults/v/items/i"}],
            "public.sources entry: unsupported field itemPath",
        ),
        (
            [{"bucket": "hikes", "r2Prefix": "hikes", "copyKeys": []}],
            "public.sources entry: unsupported field copyKeys",
        ),
        # Its names would replace the root mirror's Secrets.
        (
            [{"bucket": "hikes", "r2Prefix": "factory-public"}],
            "bucket Secret name collision consumer-factory-public-mirror-s3",
        ),
    ],
)
def test_sources_refuse_bad_entries(chart, sources, error):
    config = bucket_config()
    config["buckets"][0]["public"]["sources"] = sources
    result = render(chart, config)
    assert result.returncode != 0
    assert "factory-library: " + error in result.stderr


@pytest.mark.parametrize(
    "field,value", [("r2Prefix", "hikes"), ("excludePrefixes", ["hikes"])]
)
def test_prefixes_come_only_from_sources(chart, field, value):
    # A prefix outside sources would escape the derived root excludes.
    config = bucket_config()
    config["buckets"][0]["public"][field] = value
    result = render(chart, config)
    assert result.returncode != 0
    assert f"factory-library: public bucket: unsupported field {field}" in (
        result.stderr
    )


def test_source_mirror_name_is_reserved(chart):
    config = bucket_config()
    config["buckets"][0]["public"]["sources"] = [{"bucket": "data", "r2Prefix": "app"}]
    config["workloads"]["consumer-app-mirror"] = {"resources": copy.deepcopy(RESOURCES)}
    result = render(chart, config)
    assert result.returncode != 0
    assert "reserved mirror workload name consumer-app-mirror" in result.stderr


def test_namespace_policy_override(chart):
    config = values()
    policy = {
        "hard": {"pods": "32"},
        "defaultRequest": {"cpu": "100m"},
        "default": {"memory": "1Gi"},
    }
    config["factory-library"] = {"namespacePolicy": policy}
    docs = objects(render(chart, config))
    assert docs["ResourceQuota/consumer"]["spec"]["hard"] == {
        "requests.cpu": "2",
        "requests.memory": "4Gi",
        "limits.memory": "8Gi",
        "pods": "32",
    }
    assert docs["LimitRange/consumer"]["spec"]["limits"] == [
        {
            "type": "Container",
            "defaultRequest": {"cpu": "100m", "memory": "64Mi"},
            "default": {"memory": "1Gi"},
        }
    ]
    policy["enabled"] = False
    docs = objects(render(chart, config))
    assert {"ResourceQuota/consumer", "LimitRange/consumer"}.isdisjoint(docs)
