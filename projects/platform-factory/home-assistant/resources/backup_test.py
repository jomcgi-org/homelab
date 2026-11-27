"""Check the volume backups: scoped Garage key, both policy ends, the mounts."""

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
NS = "k8s:io.kubernetes.pod.namespace"
SECRET = "home-assistant-backups-s3"
BUCKET = "home-assistant-backups"
BACKUP = {NS: "home-assistant", "app.kubernetes.io/name": "home-assistant-backup"}
GARAGE = {NS: "garage", "app.kubernetes.io/name": "garage"}


def read(path):
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


BACKUP_DOCS = read(HERE / "backup.yaml")
CRONJOBS = {doc["metadata"]["name"]: doc for doc in BACKUP_DOCS if doc["kind"] == "CronJob"}
# Policies in this namespace have no metadata.namespace; kustomize sets it.
POLICIES = [
    {**doc, "metadata": {"namespace": "home-assistant", **doc["metadata"]}}
    for path in ("backup.yaml", "home-assistant.yaml", "zigbee2mqtt.yaml")
    for doc in read(HERE / path)
    if doc["kind"] == "CiliumNetworkPolicy"
] + read(ROOT / "garage/resources/network-policy.yaml")


def selects(selector, labels, namespace):
    expected = {NS: namespace, **selector["matchLabels"]}
    return expected.items() <= labels.items()


def allows(source, destination, port, direction):
    local, peer = (
        (source, destination) if direction == "egress" else (destination, source)
    )
    key = "toEndpoints" if direction == "egress" else "fromEndpoints"
    for policy in POLICIES:
        namespace = policy["metadata"]["namespace"]
        spec = policy["spec"]
        if not selects(spec["endpointSelector"], local, namespace):
            continue
        for rule in spec.get(direction, []):
            if any(selects(s, peer, namespace) for s in rule.get(key, [])) and any(
                entry == {"port": str(port), "protocol": "TCP"}
                for group in rule.get("toPorts", [])
                for entry in group["ports"]
            ):
                return True
    return False


def test_backup_pods_reach_only_the_garage_s3_api():
    assert allows(BACKUP, GARAGE, 3900, "egress")
    assert allows(BACKUP, GARAGE, 3900, "ingress")
    assert not allows(BACKUP, GARAGE, 3903, "egress")
    (policy,) = [
        doc for doc in BACKUP_DOCS if doc["kind"] == "CiliumNetworkPolicy"
    ]
    assert policy["spec"]["enableDefaultDeny"] == {"ingress": True, "egress": True}
    assert "ingress" not in policy["spec"]
    assert len(policy["spec"]["egress"]) == 1


@pytest.mark.parametrize("app", ["home-assistant", "zigbee2mqtt"])
def test_apps_get_no_garage_access(app):
    labels = {NS: "home-assistant", "app.kubernetes.io/name": app}
    assert not allows(labels, GARAGE, 3900, "egress")
    assert not allows(labels, GARAGE, 3900, "ingress")


def test_backup_key_is_scoped_and_kept():
    spec = importlib.util.spec_from_file_location(
        "garage_init", ROOT / "garage/resources/garage_init.py"
    )
    garage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(garage)
    assert BUCKET in garage.BUCKETS
    assert [g for g in garage.GRANTS if g[0] == BUCKET] == [
        (BUCKET, "home-assistant", SECRET)
    ]
    docs = {doc["kind"]: doc for doc in BACKUP_DOCS}
    secret = docs["Secret"]
    assert secret["metadata"]["name"] == SECRET
    assert secret["metadata"]["annotations"]["argocd.argoproj.io/sync-options"] == (
        "Prune=false,Delete=false"
    )
    assert "data" not in secret and "stringData" not in secret
    assert docs["Role"]["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": [SECRET],
            "verbs": ["get", "update"],
        }
    ]
    assert docs["RoleBinding"]["roleRef"]["name"] == docs["Role"]["metadata"]["name"]
    assert docs["RoleBinding"]["subjects"] == [
        {"kind": "ServiceAccount", "name": "garage-init", "namespace": "garage"}
    ]


def test_application_keeps_the_minted_key():
    app = read(ROOT / "home-assistant/application.yaml")[0]["spec"]
    assert {
        "kind": "Secret",
        "namespace": "home-assistant",
        "name": SECRET,
        "jsonPointers": ["/data"],
    } in app["ignoreDifferences"]
    assert "RespectIgnoreDifferences=true" in app["syncPolicy"]["syncOptions"]


def app_claims():
    """PVC name of each app Deployment, from its own manifest."""
    claims = {}
    for path in ("home-assistant.yaml", "zigbee2mqtt.yaml"):
        for doc in read(HERE / path):
            if doc["kind"] == "Deployment":
                for volume in doc["spec"]["template"]["spec"]["volumes"]:
                    if "persistentVolumeClaim" in volume:
                        claims[doc["metadata"]["name"]] = volume[
                            "persistentVolumeClaim"
                        ]["claimName"]
    return claims


def pod_of(job):
    return job["spec"]["jobTemplate"]["spec"]["template"]["spec"]


def check_pod(job, app, claim, read_only):
    pod = pod_of(job)
    # The local-path volume's node affinity places the pod.
    assert "nodeSelector" not in pod and "affinity" not in pod
    assert pod["automountServiceAccountToken"] is False
    assert pod["serviceAccountName"] == "home-assistant-backup"
    (data,) = [v for v in pod["volumes"] if "persistentVolumeClaim" in v]
    want = {"claimName": claim, **({"readOnly": True} if read_only else {})}
    assert data["persistentVolumeClaim"] == want
    (container,) = pod["containers"]
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    mount = {m["name"]: m for m in container["volumeMounts"]}[data["name"]]
    assert mount.get("readOnly", False) is read_only
    env = {e["name"]: e for e in container["env"]}
    assert env["NAME"]["value"] == app
    assert int(env["KEEP_DAYS"]["value"]) >= 14
    refs = {
        e["valueFrom"]["secretKeyRef"]["name"]
        for e in container["env"]
        if "valueFrom" in e
    }
    assert refs == {SECRET}
    return container


def test_each_app_volume_has_a_backup_and_a_restore():
    claims = app_claims()
    assert set(CRONJOBS) == {
        f"{mode}-{app}" for app in claims for mode in ("backup", "restore")
    }
    for app, claim in claims.items():
        backup = CRONJOBS[f"backup-{app}"]
        assert backup["spec"]["concurrencyPolicy"] == "Forbid"
        assert not backup["spec"].get("suspend", False)
        container = check_pod(backup, app, claim, read_only=True)
        assert container["command"][-1] == "backup"

        restore = CRONJOBS[f"restore-{app}"]
        assert restore["spec"]["suspend"] is True
        assert restore["spec"]["schedule"] == "0 0 31 2 *"
        assert restore["spec"]["jobTemplate"]["spec"]["backoffLimit"] == 0
        container = check_pod(restore, app, claim, read_only=False)
        assert container["command"][-1] == "restore"


def test_zigbee_archive_must_hold_the_network():
    env = {
        e["name"]: e.get("value")
        for e in CRONJOBS["backup-zigbee2mqtt"]["spec"]["jobTemplate"]["spec"][
            "template"
        ]["spec"]["containers"][0]["env"]
    }
    assert set(env["REQUIRED"].split()) >= {
        "configuration.yaml",
        "database.db",
        "coordinator_backup.json",
    }


def test_kustomization_ships_the_backup():
    kustomization = read(HERE / "kustomization.yaml")[0]
    assert "backup.yaml" in kustomization["resources"]
    generators = {g["name"]: g for g in kustomization["configMapGenerator"]}
    assert generators["home-assistant-backup"]["files"] == ["backup.sh"]
