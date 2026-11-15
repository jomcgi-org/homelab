"""Archive read boundary and append-only release publication regressions."""

from __future__ import annotations

import gzip
import io
import json
import subprocess
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from artifact import public_assets as reader
from artifact import publish_public_assets as writer

PATH = "entry/start.AbCd123_.js"
BODY = b'console.log("public");'
# Valid Brotli encodings of BODY with lgwin=22 and lgwin=10. Keeping these
# fixtures avoids adding a Brotli dependency to the publisher or its tests.
BROTLI = bytes.fromhex("8b0a80636f6e736f6c652e6c6f6728227075626c696322293b03")
BROTLI_OTHER = bytes.fromhex("21540004636f6e736f6c652e6c6f6728227075626c696322293b03")


class StorageError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}
        super().__init__(code)


class Storage:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.rules = []
        self.lifecycle_error = None
        self.fail_put = None
        self.race = None
        self.corrupt_read = None

    def get_bucket_lifecycle_configuration(self, **kwargs):
        self.calls.append(("lifecycle", kwargs))
        if self.lifecycle_error:
            raise StorageError(self.lifecycle_error)
        return {"Rules": self.rules}

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs))
        key = kwargs["Key"]
        if key not in self.objects:
            raise StorageError("NoSuchKey")
        body = self.objects[key]
        if self.corrupt_read == key:
            body = b"corrupt"
        return {"Body": io.BytesIO(body), "ContentLength": len(body)}

    def put_object(self, **kwargs):
        self.calls.append(("put", kwargs))
        assert kwargs["IfNoneMatch"] == "*"
        key = kwargs["Key"]
        if self.fail_put and self.fail_put(key):
            raise StorageError("AccessDenied")
        if self.race:
            self.objects[key] = self.race(kwargs["Body"])
            self.race = None
        if key in self.objects:
            raise StorageError("PreconditionFailed")
        self.objects[key] = kwargs["Body"]
        return {"ETag": '"fake"'}


@pytest.fixture
def source(tmp_path):
    path = tmp_path / PATH
    path.parent.mkdir()
    path.write_bytes(BODY)
    path.with_suffix(".js.gz").write_bytes(gzip.compress(BODY, mtime=0))
    return tmp_path


@pytest.fixture
def storage():
    return Storage()


@pytest.fixture
def client(source, storage, monkeypatch):
    writer.publish(source, client=storage, bucket="artifacts")
    monkeypatch.setattr(reader, "_client", lambda: storage)
    monkeypatch.setattr(reader, "_bucket", lambda: "artifacts")
    app = FastAPI()
    app.include_router(reader.router)
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize(
    "path",
    [
        "chunks/AbCd123_.js",
        "nodes/26.AbCd123_.js",
        PATH,
        "assets/map-theme.AbCd123_.css",
        "workers/maplibre-gl-worker-AbCd123_.js",
        "assets/photo.AbCd123_.webp",
        "assets/font.AbCd123_.woff2",
    ],
)
def test_known_hashed_paths(path):
    assert reader.valid_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "../private.js",
        "/" + PATH,
        "entry/../start.AbCd123_.js",
        "entry\\start.AbCd123_.js",
        "entry/%2e%2e.js",
        "entry/start.js",
        "private/start.AbCd123_.js",
        "server/start.AbCd123_.js",
        "entry/start.AbCd123_.js.map",
        "version.json",
        "entry/start.AbCd123_.html",
        "nodes/26.AbCd123_.js.functions.json",
        "entry/start.AbCd123_.js.gz",
        "entry/sub/start.AbCd123_.js",
        "entry/start.AbCd123_.js?bucket=private",
        "entry/start.AbCd123_.js\x00",
        "entry/..AbCd123_.js",
    ],
)
def test_rejects_non_asset_paths(path):
    assert not reader.valid_path(path)
    with pytest.raises(ValueError):
        reader.entry_key(path)


def test_publish_marker_last_and_all_representations_verified(source, storage):
    build = writer.publish(source, client=storage, bucket="artifacts")
    puts = [kw for op, kw in storage.calls if op == "put"]
    assert puts[-1]["Key"] == reader.PREFIX + "builds/" + build + ".json"
    manifest = json.loads(puts[-1]["Body"])
    assert reader.digest(puts[-1]["Body"]) == build
    assert manifest["entries"][PATH]["variants"]["identity"]["sha256"] == reader.digest(
        BODY
    )
    assert set(manifest["entries"][PATH]["variants"]) == {"identity", "gzip"}
    entry_put = next(
        i
        for i, (op, kw) in enumerate(storage.calls)
        if op == "put" and "/entries/" in kw["Key"]
    )
    for kw in puts[:-2]:
        assert any(
            op == "get" and read["Key"] == kw["Key"]
            for op, read in storage.calls[:entry_put]
        )
    before = dict(storage.objects)
    assert writer.publish(source, client=storage, bucket="artifacts") == build
    assert storage.objects == before
    assert len([1 for op, _ in storage.calls if op == "put"]) == len(puts)


def test_partial_upload_has_no_manifest_and_retry_completes(source, storage):
    storage.fail_put = lambda key: "/objects/" in key and len(storage.objects) == 1
    with pytest.raises(StorageError):
        writer.publish(source, client=storage, bucket="artifacts")
    assert not any("/entries/" in key or "/builds/" in key for key in storage.objects)
    original = dict(storage.objects)
    storage.fail_put = None
    build = writer.publish(source, client=storage, bucket="artifacts")
    assert all(storage.objects[k] == v for k, v in original.items())
    assert reader.PREFIX + "builds/" + build + ".json" in storage.objects


def test_path_collision_never_replaces_existing_bytes(source, storage):
    writer.publish(source, client=storage, bucket="artifacts")
    original_entry = storage.objects[reader.entry_key(PATH)]
    (source / PATH).write_bytes(b"different public build at same hashed filename")
    with pytest.raises(ValueError):
        writer.publish(source, client=storage, bucket="artifacts")
    assert storage.objects[reader.entry_key(PATH)] == original_entry
    assert len([key for key in storage.objects if "/builds/" in key]) == 1


def test_recompression_retains_verified_representations_and_completion(source, storage):
    (source / (PATH + ".br")).write_bytes(BROTLI)
    first = writer.publish(source, client=storage, bucket="artifacts")
    stored = dict(storage.objects)
    puts = len([op for op, _ in storage.calls if op == "put"])
    (source / (PATH + ".gz")).write_bytes(gzip.compress(BODY, mtime=42))
    (source / (PATH + ".br")).write_bytes(BROTLI_OTHER)
    assert writer.publish(source, client=storage, bucket="artifacts") == first
    assert storage.objects == stored
    assert len([op for op, _ in storage.calls if op == "put"]) == puts
    manifest = json.loads(storage.objects[reader.PREFIX + "builds/" + first + ".json"])
    assert manifest["entries"][PATH] == json.loads(stored[reader.entry_key(PATH)])


def test_existing_variant_set_remains_sealed_when_build_adds_or_drops_encoding(
    source, storage
):
    first = writer.publish(source, client=storage, bucket="artifacts")
    stored = dict(storage.objects)
    (source / (PATH + ".gz")).unlink()
    (source / (PATH + ".br")).write_bytes(BROTLI)
    assert writer.publish(source, client=storage, bucket="artifacts") == first
    assert storage.objects == stored
    assert set(json.loads(stored[reader.entry_key(PATH)])["variants"]) == {
        "identity",
        "gzip",
    }


@pytest.mark.parametrize("encoding", ["identity", "gzip", "br"])
@pytest.mark.parametrize("broken", ["missing", "corrupt"])
def test_invalid_sealed_object_fails_before_any_repair_or_upload(
    source, storage, encoding, broken
):
    (source / (PATH + ".br")).write_bytes(BROTLI)
    writer.publish(source, client=storage, bucket="artifacts")
    entry = json.loads(storage.objects[reader.entry_key(PATH)])
    key = reader.object_key(entry["variants"][encoding]["sha256"])
    if broken == "missing":
        del storage.objects[key]
    else:
        storage.objects[key] = b"corrupt"
    broken_archive = dict(storage.objects)
    puts = len([op for op, _ in storage.calls if op == "put"])
    (source / (PATH + ".gz")).write_bytes(gzip.compress(BODY, mtime=42))
    with pytest.raises((StorageError, ValueError)):
        writer.publish(source, client=storage, bucket="artifacts")
    assert storage.objects == broken_archive
    assert len([op for op, _ in storage.calls if op == "put"]) == puts


@pytest.mark.parametrize(
    "conflict", ["PreconditionFailed", "ConditionalRequestConflict"]
)
@pytest.mark.parametrize(
    "winner",
    ["same", "identity-collision", "missing-gzip", "corrupt-gzip", "bad-schema"],
)
def test_concurrent_seal_adopts_only_fully_verified_same_identity_winner(
    source, storage, monkeypatch, conflict, winner
):
    identity = BODY if winner != "identity-collision" else b"different identity"
    variants = {"identity": identity, "gzip": gzip.compress(identity, mtime=42)}
    entry = {
        "version": 1,
        "path": PATH,
        "variants": {
            encoding: {"sha256": reader.digest(body), "size": len(body)}
            for encoding, body in variants.items()
        },
    }
    if winner == "bad-schema":
        entry["unrecognized"] = True
    original_put = storage.put_object

    def raced_put(**kwargs):
        if kwargs["Key"] != reader.entry_key(PATH):
            return original_put(**kwargs)
        assert kwargs["IfNoneMatch"] == "*"
        for encoding, body in variants.items():
            if encoding == "gzip" and winner == "missing-gzip":
                continue
            storage.objects[reader.object_key(reader.digest(body))] = (
                b"corrupt" if encoding == "gzip" and winner == "corrupt-gzip" else body
            )
        storage.objects[kwargs["Key"]] = reader.canonical(entry)
        raise StorageError(conflict)

    monkeypatch.setattr(storage, "put_object", raced_put)
    if winner == "same":
        build = writer.publish(source, client=storage, bucket="artifacts")
        manifest = json.loads(
            storage.objects[reader.PREFIX + "builds/" + build + ".json"]
        )
        assert manifest["entries"][PATH] == entry
        assert manifest["entries"][PATH]["variants"]["gzip"]["sha256"] != reader.digest(
            (source / (PATH + ".gz")).read_bytes()
        )
    else:
        with pytest.raises((StorageError, ValueError)):
            writer.publish(source, client=storage, bucket="artifacts")
        assert not any("/builds/" in key for key in storage.objects)
    assert storage.objects[reader.entry_key(PATH)] == reader.canonical(entry)


@pytest.mark.parametrize("same", [True, False])
def test_conditional_create_race_checks_winner(storage, same):
    storage.race = (lambda body: body) if same else (lambda body: b"different")
    key = reader.object_key(reader.digest(BODY))
    if same:
        writer.append_verified(
            storage, "artifacts", key, BODY, "application/octet-stream"
        )
    else:
        with pytest.raises(ValueError):
            writer.append_verified(
                storage, "artifacts", key, BODY, "application/octet-stream"
            )
    assert storage.objects[key] == (BODY if same else b"different")
    assert len([1 for op, _ in storage.calls if op == "put"]) == 1


def test_corrupt_readback_blocks_completion(source, storage):
    storage.corrupt_read = reader.object_key(reader.digest(BODY))
    with pytest.raises(ValueError):
        writer.publish(source, client=storage, bucket="artifacts")
    assert not any("/builds/" in key for key in storage.objects)


def test_unsupported_conditional_create_never_falls_back(source, storage, monkeypatch):
    def unsupported(**kwargs):
        assert kwargs["IfNoneMatch"] == "*"
        raise StorageError("NotImplemented")

    monkeypatch.setattr(storage, "put_object", unsupported)
    with pytest.raises(StorageError, match="NotImplemented"):
        writer.publish(source, client=storage, bucket="artifacts")
    assert not storage.objects


def test_failed_completion_is_retryable_without_replacing_entries(source, storage):
    storage.fail_put = lambda key: "/builds/" in key
    with pytest.raises(StorageError):
        writer.publish(source, client=storage, bucket="artifacts")
    assert reader.entry_key(PATH) in storage.objects
    assert not any("/builds/" in key for key in storage.objects)
    sealed = dict(storage.objects)
    storage.fail_put = None
    writer.publish(source, client=storage, bucket="artifacts")
    assert all(storage.objects[key] == value for key, value in sealed.items())


@pytest.mark.parametrize("code", ["NotImplemented", "NoSuchBucket", "InternalError"])
def test_lifecycle_unknown_denies_all_writes(source, storage, code):
    storage.lifecycle_error = code
    with pytest.raises(RuntimeError, match="cannot establish"):
        writer.publish(source, client=storage, bucket="artifacts")
    assert not storage.objects


def test_no_lifecycle_configuration_is_explicitly_supported(source, storage):
    storage.lifecycle_error = "NoSuchLifecycleConfiguration"
    writer.publish(source, client=storage, bucket="artifacts")


def test_lifecycle_permission_limit_is_recorded_without_weakening_object_checks(
    source, storage, caplog
):
    storage.lifecycle_error = "AccessDenied"
    build = writer.publish(source, client=storage, bucket="artifacts")
    manifest = json.loads(storage.objects[reader.PREFIX + "builds/" + build + ".json"])
    assert manifest["lifecycle"] == {
        "status": "unverified",
        "reason": "configuration-access-denied",
    }
    assert "lifecycle-unverified" in caplog.text
    storage.fail_put = lambda key: True
    (source / "entry/new.AbCd123_.js").write_bytes(b"new")
    with pytest.raises(StorageError, match="AccessDenied"):
        writer.publish(source, client=storage, bucket="artifacts")


@pytest.mark.parametrize(
    "filt",
    [
        {},
        {"Prefix": ""},
        {"Prefix": "public-frontend/"},
        {"Prefix": reader.PREFIX + "objects/"},
        {"And": {"Prefix": reader.PREFIX, "Tags": [{"Key": "future", "Value": "tag"}]}},
    ],
)
def test_possible_prefix_expiration_fails(source, storage, filt):
    storage.rules = [{"Status": "Enabled", "Filter": filt, "Expiration": {"Days": 30}}]
    with pytest.raises(ValueError, match="lifecycle"):
        writer.publish(source, client=storage, bucket="artifacts")
    assert not storage.objects


@pytest.mark.parametrize(
    "rule",
    [
        {"Status": "Disabled", "Expiration": {"Days": 1}},
        {"Status": "Enabled", "Prefix": "unrelated/", "Expiration": {"Days": 1}},
        {
            "Status": "Enabled",
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
        },
    ],
)
def test_non_expiring_policy_allowed(source, storage, rule):
    storage.rules = [rule]
    writer.publish(source, client=storage, bucket="artifacts")


@pytest.mark.parametrize(
    "rule",
    [
        {"Status": "Unknown"},
        {"Status": "Enabled", "Filter": "invalid"},
        {"Status": "Enabled", "Filter": {"And": "invalid"}},
        {"Status": "Enabled", "Filter": {"Prefix": 42}, "Expiration": {"Days": 1}},
        {"Status": "Enabled", "Transitions": [{"StorageClass": "GLACIER", "Days": 1}]},
    ],
)
def test_unknown_or_unreadable_lifecycle_fails(source, storage, rule):
    storage.rules = [rule]
    with pytest.raises((TypeError, ValueError)):
        writer.publish(source, client=storage, bucket="artifacts")
    assert not storage.objects


def test_links_or_orphan_compression_are_rejected(source, storage):
    (source / "chunks").symlink_to(source / "entry", target_is_directory=True)
    with pytest.raises(ValueError, match="link"):
        writer.publish(source, client=storage, bucket="artifacts")
    (source / "chunks").unlink()
    (source / PATH).unlink()
    with pytest.raises(ValueError, match="orphan"):
        writer.publish(source, client=storage, bucket="artifacts")


def test_bounds_and_unexpected_metadata_rejected(source, storage, monkeypatch):
    metadata = source / "nodes/26.AbCd123_.js.functions.json"
    metadata.parent.mkdir()
    metadata.write_text("{}")
    with pytest.raises(ValueError, match="outside"):
        writer.publish(source, client=storage, bucket="artifacts")
    assert not storage.objects
    metadata.unlink()
    monkeypatch.setattr(writer, "MAX_TOTAL_BYTES", 1)
    with pytest.raises(ValueError, match="bounds"):
        writer.publish(source, client=storage, bucket="artifacts")


def test_get_head_and_conditionals_are_exact(client):
    response = client.get("/internal/public-assets/" + PATH)
    assert response.status_code == 200 and response.content == BODY
    assert response.headers["content-type"] == "text/javascript"
    assert response.headers["cache-control"] == reader.CACHE_CONTROL
    assert response.headers["etag"] == f'"{reader.digest(BODY)}"'
    assert response.headers["content-length"] == str(len(BODY))
    assert response.headers["x-content-type-options"] == "nosniff"
    head = client.head("/internal/public-assets/" + PATH)
    assert head.content == b"" and head.headers == response.headers
    cached = client.get(
        "/internal/public-assets/" + PATH,
        headers={"If-None-Match": "W/" + response.headers["etag"]},
    )
    assert cached.status_code == 304 and cached.content == b""
    assert "content-length" not in cached.headers


def test_compressed_bytes_and_missing_representation(client):
    with client.stream(
        "GET", "/internal/public-assets/" + PATH + "?encoding=gzip"
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-encoding"] == "gzip"
        assert b"".join(response.iter_raw()) == gzip.compress(BODY, mtime=0)
    response = client.get("/internal/public-assets/" + PATH + "?encoding=br")
    assert (
        response.status_code == 406 and response.headers["cache-control"] == "no-store"
    )


@pytest.mark.parametrize(
    "suffix",
    ["?bucket=private", "?encoding=identity&encoding=br", "?encoding=bad", "/bad"],
)
def test_query_and_path_boundary_reject_before_storage(client, storage, suffix):
    storage.calls.clear()
    response = client.get("/internal/public-assets/" + PATH + suffix)
    assert (
        response.status_code == 404 and response.headers["cache-control"] == "no-store"
    )
    assert storage.calls == []


def test_unknown_file_and_corrupt_manifest_do_not_expose_objects(client, storage):
    response = client.get("/internal/public-assets/chunks/XXXXXXXX.js")
    assert (
        response.status_code == 404 and response.headers["cache-control"] == "no-store"
    )
    key = reader.entry_key(PATH)
    entry = json.loads(storage.objects[key])
    entry["variants"]["identity"]["sha256"] = "../private"
    storage.objects[key] = reader.canonical(entry)
    response = client.get("/internal/public-assets/" + PATH)
    assert (
        response.status_code == 503 and response.headers["cache-control"] == "no-store"
    )
    assert not any("private" in kwargs.get("Key", "") for op, kwargs in storage.calls)


def test_missing_or_corrupt_verified_blob_fails_closed(client, storage):
    storage.objects[reader.object_key(reader.digest(BODY))] = b"corrupt"
    response = client.get("/internal/public-assets/" + PATH)
    assert (
        response.status_code == 503 and response.headers["cache-control"] == "no-store"
    )


def test_reader_has_no_write_method(client, storage):
    storage.calls.clear()
    assert (
        client.post("/internal/public-assets/" + PATH, content=b"overwrite").status_code
        == 405
    )
    assert not storage.calls


def test_encoded_path_alias_is_rejected_before_storage(client, storage):
    storage.calls.clear()
    response = client.get("/internal/public-assets/entry/start.%41bCd123_.js")
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert not storage.calls


def test_archive_client_has_bounded_io_and_reuses_existing_identity(monkeypatch):
    import boto3

    calls = []
    sentinel = object()
    monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "https://storage.example")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "fixture-id")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "fixture-secret")
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: calls.append(kw) or sentinel)
    reader._client.cache_clear()
    try:
        assert reader._client() is sentinel
        assert reader._client() is sentinel
        assert len(calls) == 1
        assert calls[0]["aws_access_key_id"] == "fixture-id"
        assert calls[0]["aws_secret_access_key"] == "fixture-secret"
        assert calls[0]["config"].connect_timeout == 1
        assert calls[0]["config"].read_timeout == 2
        assert calls[0]["config"].retries == {"total_max_attempts": 1}
        assert calls[0]["config"].s3 == {"addressing_style": "path"}
    finally:
        reader._client.cache_clear()


def test_public_registration_does_not_import_publisher():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from fastapi import FastAPI; import artifact; artifact.register_public(FastAPI()); assert 'artifact.publish_public_assets' not in sys.modules; assert 'boto3' not in sys.modules",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout == ""
