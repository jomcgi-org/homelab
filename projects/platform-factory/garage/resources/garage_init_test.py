"""Exercise discovery and reconciliation without cluster credentials."""

import copy
import importlib.util
from pathlib import Path
import urllib.parse

import pytest

SPEC = importlib.util.spec_from_file_location(
    "garage_init", Path(__file__).with_name("garage_init.py")
)
INIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INIT)


def test_discovery_paginates_and_skips_namespaces_without_permission(monkeypatch):
    calls = []

    def k8s(method, path, body=None, metadata_only=False):
        assert method == "GET" and body is None and metadata_only
        url = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qs(url.query)
        calls.append((url.path, query))
        assert query["limit"] == ["100"]
        if url.path == "/api/v1/namespaces":
            if "continue" not in query:
                return {
                    "metadata": {"continue": "namespace-page"},
                    "items": [{"metadata": {"name": "private"}}],
                }
            return {"items": [{"metadata": {"name": "public"}}]}
        assert query["labelSelector"] == [INIT.CLAIM_LABEL]
        assert url.path.endswith("/configmaps")
        if url.path == "/api/v1/namespaces/private/configmaps":
            return None  # GET 403, no list Role in this namespace.
        if "continue" not in query:
            return {"metadata": {"continue": "claim-page"}, "items": []}
        return {
            "items": [
                {
                    "metadata": {
                        "name": "mirror-s3",
                        "labels": {
                            INIT.CLAIM_LABEL: "factory-public",
                            INIT.ACCESS_LABEL: "read",
                        },
                    }
                }
            ]
        }

    monkeypatch.setattr(INIT, "k8s", k8s)
    assert list(INIT.discover_grants()) == [
        ("factory-public", "public", "mirror-s3", "read")
    ]
    assert len(calls) == 5


@pytest.mark.parametrize("access", ["read", "write", "readwrite"])
@pytest.mark.parametrize("existing", [False, True])
def test_grant_is_stable_and_revokes_unwanted_permissions(
    monkeypatch, access, existing
):
    original = {
        "metadata": {"resourceVersion": "1"},
        "data": {"other": INIT.b64("preserve")},
    }
    if existing:
        original["data"].update(
            AWS_ACCESS_KEY_ID=INIT.b64("GK-existing"),
            AWS_SECRET_ACCESS_KEY=INIT.b64("existing-secret"),
        )
    state = copy.deepcopy(original)
    calls = []
    imported = None

    def k8s(method, path, body=None):
        assert path == INIT.secret_path("consumer", "bucket-s3")
        if method == "PUT":
            assert body["metadata"]["resourceVersion"] == "1"
            state.update(copy.deepcopy(body))
        else:
            assert method == "GET"
        return copy.deepcopy(state)

    def garage(method, endpoint, body=None, **kwargs):
        nonlocal imported
        calls.append((method, endpoint, body))
        if endpoint == "GetKeyInfo":
            if imported:
                return imported
            if existing:
                return {
                    "secretAccessKey": "existing-secret",
                    "buckets": [{"id": "old-bucket"}],
                }
            return None
        if endpoint == "ImportKey":
            # Persistence must precede Garage creation for crash recovery.
            assert INIT.unb64(state["data"]["AWS_ACCESS_KEY_ID"]) == body["accessKeyId"]
            imported = {"secretAccessKey": body["secretAccessKey"]}
        return {}

    monkeypatch.setattr(INIT, "k8s", k8s)
    monkeypatch.setattr(INIT, "garage", garage)
    INIT.ensure_grant("bucket-id", "factory-public", "consumer", "bucket-s3", access)
    first = copy.deepcopy(state)
    INIT.ensure_grant("bucket-id", "factory-public", "consumer", "bucket-s3", access)
    assert state == first
    assert state["data"]["other"] == original["data"]["other"]
    if existing:
        assert (
            state["data"]["AWS_ACCESS_KEY_ID"] == original["data"]["AWS_ACCESS_KEY_ID"]
        )
        assert not any(call[1] == "ImportKey" for call in calls)
        assert any(
            endpoint == "DenyBucketKey"
            and body["bucketId"] == "old-bucket"
            and body["permissions"] == {"read": True, "write": True, "owner": True}
            for _, endpoint, body in calls
        )
    else:
        assert sum(endpoint == "ImportKey" for _, endpoint, _ in calls) == 1
    grants = [body for _, endpoint, body in calls if endpoint == "AllowBucketKey"]
    expected = {
        "read": access in ("read", "readwrite"),
        "write": access in ("write", "readwrite"),
        "owner": False,
    }
    assert all(grant["permissions"] == expected for grant in grants)
    denies = [
        body
        for _, endpoint, body in calls
        if endpoint == "DenyBucketKey" and body["bucketId"] == "bucket-id"
    ]
    assert all(
        deny["permissions"] == {key: not value for key, value in expected.items()}
        for deny in denies
    )


def test_partial_credentials_never_rotate(monkeypatch):
    monkeypatch.setattr(
        INIT, "k8s", lambda *args: {"data": {"AWS_ACCESS_KEY_ID": INIT.b64("existing")}}
    )
    monkeypatch.setattr(
        INIT, "garage", lambda *args, **kwargs: pytest.fail("no Garage write")
    )
    with pytest.raises(RuntimeError, match="incomplete key pair"):
        INIT.ensure_grant(
            "bucket-id", "factory-public", "consumer", "bucket-s3", "read"
        )


def test_missing_consumer_secret_is_skipped(monkeypatch):
    monkeypatch.setattr(INIT, "k8s", lambda *args: None)
    monkeypatch.setattr(
        INIT, "garage", lambda *args, **kwargs: pytest.fail("no Garage write")
    )
    INIT.ensure_grant("bucket-id", "factory-public", "consumer", "bucket-s3", "read")


def test_namespace_discovery_permission_failure_is_visible(monkeypatch):
    monkeypatch.setattr(INIT, "k8s", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="namespace discovery is not authorized"):
        list(INIT.discover_grants())


def test_discovery_rbac_and_garage_ingress():
    import yaml

    here = Path(__file__).parent
    bootstrap = list(yaml.safe_load_all((here / "bootstrap.yaml").read_text()))
    role = next(doc for doc in bootstrap if doc["kind"] == "ClusterRole")
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["namespaces"], "verbs": ["list"]}
    ]
    policy = next(
        doc
        for doc in yaml.safe_load_all((here / "network-policy.yaml").read_text())
        if doc["metadata"]["name"] == "garage"
    )
    claims = [
        rule
        for rule in policy["spec"]["ingress"]
        if any(
            endpoint.get("matchLabels", {}).get("factory.jomcgi.dev/garage-client")
            == "true"
            for endpoint in rule.get("fromEndpoints", [])
        )
    ]
    assert len(claims) == 1
    assert claims[0]["toPorts"] == [{"ports": [{"port": "3900", "protocol": "TCP"}]}]
    selector = claims[0]["fromEndpoints"][0]
    assert selector["matchExpressions"] == [
        {"key": "k8s:io.kubernetes.pod.namespace", "operator": "Exists"}
    ]
