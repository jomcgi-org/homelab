"""Backfill configuration and image-copy tests (fake S3, never the network)."""

import pytest
from botocore.exceptions import ClientError
from typer.testing import CliRunner

from trips.backfill import main


@pytest.fixture
def s3_config(monkeypatch):
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "synthetic-access")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "synthetic-secret")
    monkeypatch.delenv("AWS_REGION", raising=False)
    calls = []

    def client(*args, **kwargs):
        calls.append((args, kwargs))
        return "fake-client"

    monkeypatch.setattr(main.boto3, "client", client)
    return calls


@pytest.mark.parametrize("region", [None, "auto", "test-region"])
@pytest.mark.parametrize(
    "endpoint,expected",
    [
        ("https://fixture.r2.invalid", "https://fixture.r2.invalid"),
        ("http://test.invalid:8333", "http://test.invalid:8333"),
        ("test.invalid:8333", "http://test.invalid:8333"),
    ],
)
def test_s3_client_uses_explicit_shared_identity(
    s3_config, monkeypatch, region, endpoint, expected
):
    if region is not None:
        monkeypatch.setenv("AWS_REGION", region)
    assert main.get_s3_client(endpoint) == "fake-client"
    args, kwargs = s3_config[0]
    assert args == ("s3",)
    assert kwargs["endpoint_url"] == expected
    assert kwargs["aws_access_key_id"] == "synthetic-access"
    assert kwargs["aws_secret_access_key"] == "synthetic-secret"
    assert kwargs["region_name"] == (region or "auto")
    assert kwargs["config"].signature_version == "s3v4"


@pytest.mark.parametrize("key", ["S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"])
@pytest.mark.parametrize("value", [None, "", " "])
def test_missing_shared_credentials_fail_before_client(
    s3_config, monkeypatch, key, value
):
    if value is None:
        monkeypatch.delenv(key)
    else:
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match=f"{key} is required"):
        main.get_s3_client("https://fixture.r2.invalid")
    assert s3_config == []


def test_missing_endpoint_fails_before_client(s3_config):
    with pytest.raises(
        ValueError, match="--endpoint or SEAWEEDFS_S3_ENDPOINT is required"
    ):
        main.get_s3_client("")
    assert s3_config == []


@pytest.mark.parametrize("override", [False, True])
def test_cli_endpoint_environment_and_explicit_precedence(monkeypatch, override):
    monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "https://environment.r2.invalid")
    calls = []

    def stop_at_client(endpoint):
        calls.append(endpoint)
        raise RuntimeError("stop before I/O")

    monkeypatch.setattr(main, "get_s3_client", stop_at_client)
    args = ["--slug", "fixture-trip"]
    if override:
        args += ["--endpoint", "https://explicit.r2.invalid"]
    result = CliRunner().invoke(main.app, args)
    assert isinstance(result.exception, RuntimeError)
    assert str(result.exception) == "stop before I/O"
    assert calls == [
        "https://explicit.r2.invalid" if override else "https://environment.r2.invalid"
    ]


def test_operator_yaml_schema_and_defaults(tmp_path):
    config = tmp_path / "operator trip.yaml"
    config.write_text("""
trip: {title: Example, short_title: Short, subtitle: A trip}
timeline: {default_image: img_example.jpg, default_zoom: 8}
days: {2025-01-03: {label: Arrival}}
highlights: [{label: View}]
stats: {distance: 42}
""")
    trip = main._load_trip_row("fixture", config, "Europe/London")
    assert (trip.slug, trip.tz) == ("fixture", "Europe/London")
    assert (trip.title, trip.short_title, trip.subtitle) == (
        "Example",
        "Short",
        "A trip",
    )
    assert (trip.default_image, trip.default_zoom) == ("img_example.jpg", 8)
    assert trip.days == {"2025-01-03": {"label": "Arrival"}}
    assert trip.highlights == [{"label": "View"}]
    assert trip.stats == {"distance": 42}
    config.write_text("{}")
    empty = main._load_trip_row("fixture", config, "UTC")
    assert empty.title == "fixture"
    assert (
        empty.short_title
        is empty.subtitle
        is empty.default_image
        is empty.default_zoom
        is None
    )
    assert empty.days == empty.stats == {}
    assert empty.highlights == []


class _FakeS3:
    """Minimal S3 stub recording head_object / copy_object calls.

    Keys in ``present`` already exist in the destination, so ``head_object``
    succeeds (the copy is skipped); any other key raises a 404 ClientError to
    mimic a destination miss.
    """

    def __init__(self, present=None):
        self.present = set(present or [])
        self.copied: list[tuple[str, str, str]] = []  # (src_bucket, key, dest_bucket)
        self.head_calls: list[str] = []

    def head_object(self, *, Bucket, Key):
        self.head_calls.append(Key)
        if Key in self.present:
            return {"ContentLength": 1}
        raise ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )

    def copy_object(self, *, Bucket, Key, CopySource):
        self.copied.append((CopySource["Bucket"], CopySource["Key"], Bucket))
        self.present.add(Key)


def test_copy_images_copies_each_key():
    s3 = _FakeS3()
    copied, skipped = main._copy_images(
        s3, "trips", "monolith-trips", ["a.jpg", "b.jpg"]
    )
    assert (copied, skipped) == (2, 0)
    # Concurrency means order is nondeterministic; sort by key.
    assert sorted(c[1] for c in s3.copied) == ["a.jpg", "b.jpg"]
    assert all(c[0] == "trips" and c[2] == "monolith-trips" for c in s3.copied)


def test_copy_images_skips_present_dest_key():
    s3 = _FakeS3(present=["a.jpg"])
    copied, skipped = main._copy_images(
        s3, "trips", "monolith-trips", ["a.jpg", "b.jpg"]
    )
    assert (copied, skipped) == (1, 1)
    # Only the missing key was copied.
    assert [c[1] for c in s3.copied] == ["b.jpg"]


def test_copy_images_short_circuits_when_src_equals_dest():
    s3 = _FakeS3()
    copied, skipped = main._copy_images(s3, "trips", "trips", ["a.jpg", "b.jpg"])
    assert (copied, skipped) == (0, 0)
    assert s3.copied == []
    assert s3.head_calls == []


def test_looks_like_jpeg_accepts_real_magic_and_size():
    # SOI marker + padding over the 1KB floor.
    assert main._looks_like_jpeg(b"\xff\xd8" + b"\x00" * 2000)


def test_looks_like_jpeg_rejects_error_body_and_undersized():
    assert not main._looks_like_jpeg(b"operation Lookup failed")  # 67-byte-style body
    assert not main._looks_like_jpeg(b"\xff\xd8" + b"\x00" * 10)  # magic but too small
    assert not main._looks_like_jpeg(b"%PNG" + b"\x00" * 2000)  # right size, not JPEG
    assert not main._looks_like_jpeg(b"")
