"""The health publisher signs its upload and publishes component states only."""

import datetime
import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "health", Path(__file__).resolve().parent / "files" / "health.py"
)
health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health)

NOW = datetime.datetime(2026, 11, 19, 8, 0, tzinfo=datetime.timezone.utc)
ENV = {"CLOUDFLARE_API_TOKEN": "token", "CLOUDFLARE_ACCOUNT_ID": "account"}


def test_signature_matches_the_aws_example():
    # The GET Object example in the AWS Signature Version 4 documentation.
    empty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    headers = {
        "host": "examplebucket.s3.amazonaws.com",
        "range": "bytes=0-9",
        "x-amz-content-sha256": empty,
        "x-amz-date": "20130524T000000Z",
    }
    assert health.signature(
        "GET",
        "https://examplebucket.s3.amazonaws.com/test.txt",
        headers,
        empty,
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "us-east-1",
        "20130524T000000Z",
    ) == (
        "20130524/us-east-1/s3/aws4_request",
        "host;range;x-amz-content-sha256;x-amz-date",
        "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41",
    )


def test_the_document_holds_names_and_states_only():
    document = health.compose(
        {"factory-cluster": True, "jomcgi-site-worker": False}, NOW
    )
    assert document == {
        "status": "unhealthy",
        "generatedAt": "2026-11-19T08:00:00Z",
        "components": [
            {"name": "factory-cluster", "state": "up"},
            {"name": "jomcgi-site-worker", "state": "down"},
        ],
    }
    assert health.compose({"factory-cluster": True}, NOW)["status"] == "healthy"


def test_a_failed_read_publishes_down_without_its_error(monkeypatch, capsys):
    def broken(_now, _env):
        raise OSError("api unreachable at internal.example")

    uploads = []
    monkeypatch.setattr(health, "CHECKS", {"factory-cluster": broken})
    monkeypatch.setattr(health, "put", lambda body, env, now: uploads.append(body))
    health.main()
    document = json.loads(uploads[0])
    assert document["status"] == "unhealthy"
    assert document["components"] == [{"name": "factory-cluster", "state": "down"}]
    assert b"internal" not in uploads[0]
    assert "internal" not in capsys.readouterr().out


def test_cluster_needs_healthy_argocd_reconciled_in_15_minutes(monkeypatch):
    status = {"health": {"status": "Healthy"}, "reconciledAt": "2026-11-19T07:50:00Z"}
    monkeypatch.setattr(health, "kube", lambda path: {"status": status})
    assert health.cluster(NOW, {}) is True
    status["reconciledAt"] = "2026-11-19T07:44:00Z"
    assert health.cluster(NOW, {}) is False
    status["reconciledAt"] = "2026-11-19T07:59:00Z"
    status["health"]["status"] = "Degraded"
    assert health.cluster(NOW, {}) is False


def test_worker_reads_the_latest_deployment_id_and_creation_time(monkeypatch):
    seen = []
    body = {
        "success": True,
        "result": {
            "deployments": [
                {"id": "old", "created_on": "2026-11-18T08:00:00.000000Z"},
                {"id": "new", "created_on": "2026-11-19T07:00:00.000000Z"},
            ]
        },
    }

    def fake(path, env):
        seen.append(path)
        return body

    monkeypatch.setattr(health, "cloudflare", fake)
    assert health.worker(NOW, ENV) is True
    assert seen == ["/accounts/account/workers/scripts/jomcgi-site/deployments"]
    # The latest deployment decides, even when it lacks an ID.
    body["result"]["deployments"][1]["id"] = ""
    assert health.worker(NOW, ENV) is False
    # A creation time after the check time is not a deployment that happened,
    # even by a few seconds.
    deployment = body["result"]["deployments"][1]
    deployment.update(id="new", created_on="2026-11-19T08:00:00Z")
    assert health.worker(NOW, ENV) is True
    deployment["created_on"] = "2026-11-19T08:00:01Z"
    assert health.worker(NOW, ENV) is False
    deployment["created_on"] = "2026-11-19T08:02:00Z"
    assert health.worker(NOW, ENV) is False
    body["result"]["deployments"] = []
    assert health.worker(NOW, ENV) is False
    body["success"] = False
    assert health.worker(NOW, ENV) is False


def test_worker_without_its_token_is_down(monkeypatch):
    uploads = []
    monkeypatch.setattr(health, "CHECKS", {"jomcgi-site-worker": health.worker})
    monkeypatch.setattr(health, "put", lambda body, env, now: uploads.append(body))
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    health.main()
    assert json.loads(uploads[0])["components"] == [
        {"name": "jomcgi-site-worker", "state": "down"}
    ]


def test_upload_is_signed_put_to_the_health_bucket_key(monkeypatch):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout):
        seen["request"] = request
        return Response()

    monkeypatch.setattr(health, "urlopen", fake_urlopen)
    env = {
        "AWS_ENDPOINT_URL": "http://garage.garage.svc:3900",
        "BUCKET": "factory-health",
        "AWS_REGION": "garage",
        "AWS_ACCESS_KEY_ID": "GKexample",
        "AWS_SECRET_ACCESS_KEY": "secret",
    }
    health.put(b"{}", env, NOW)
    request = seen["request"]
    assert request.get_method() == "PUT"
    assert (
        request.full_url == "http://garage.garage.svc:3900/factory-health/health.json"
    )
    assert request.get_header("Authorization").startswith(
        "AWS4-HMAC-SHA256 Credential=GKexample/20261119/garage/s3/aws4_request"
    )
