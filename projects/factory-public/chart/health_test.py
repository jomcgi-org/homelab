"""The health publisher signs its upload and publishes component states only."""

import datetime
import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "health", Path(__file__).resolve().parent / "files" / "health.py"
)
health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health)

NOW = datetime.datetime(2026, 11, 18, 22, 0, tzinfo=datetime.timezone.utc)


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


def test_one_unhealthy_component_makes_the_document_unhealthy():
    document = health.compose({"factory-cluster": True, "jomcgi-site": False}, NOW)
    assert document == {
        "status": "unhealthy",
        "generatedAt": "2026-11-18T22:00:00Z",
        "components": {"factory-cluster": "healthy", "jomcgi-site": "unhealthy"},
    }
    assert health.compose({"factory-cluster": True}, NOW)["status"] == "healthy"


def test_a_failed_read_publishes_unhealthy(monkeypatch):
    def broken(_now):
        raise OSError("api unreachable")

    uploads = []
    monkeypatch.setattr(health, "CHECKS", {"factory-cluster": broken})
    monkeypatch.setattr(health, "put", lambda body, env, now: uploads.append(body))
    health.main()
    assert b'"status":"unhealthy"' in uploads[0]


def test_site_needs_the_wanted_revision_deployed(monkeypatch):
    status = {
        "health": {"status": "Healthy"},
        "sync": {"status": "Synced", "revision": "b"},
        "operationState": {"phase": "Succeeded", "syncResult": {"revision": "a"}},
    }
    monkeypatch.setattr(health, "kube", lambda path: {"status": status})
    assert health.site(NOW) is False
    status["operationState"]["syncResult"]["revision"] = "b"
    assert health.site(NOW) is True


def test_upload_is_signed_put_to_the_bucket_key(monkeypatch):
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
        "BUCKET": "factory-public",
        "AWS_REGION": "garage",
        "AWS_ACCESS_KEY_ID": "GKexample",
        "AWS_SECRET_ACCESS_KEY": "secret",
    }
    health.put(b"{}", env, NOW)
    request = seen["request"]
    assert request.get_method() == "PUT"
    assert (
        request.full_url == "http://garage.garage.svc:3900/factory-public/health.json"
    )
    assert request.get_header("Authorization").startswith(
        "AWS4-HMAC-SHA256 Credential=GKexample/20261118/garage/s3/aws4_request"
    )
