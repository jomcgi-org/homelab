"""Publication boundary: no originals/metadata, resumable renders, atomic manifest."""

import hashlib
import json
from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError

from trips.export import export_trips, signed_path, validate_webp

KEY = "img_" + "a" * 12 + ".jpg"
WEBP = (
    b"RIFF"
    + (14).to_bytes(4, "little")
    + b"WEBPVP8 "
    + (2).to_bytes(4, "little")
    + b"xx"
)
INDEX = {
    "count": 1,
    "trips": [{"slug": "2025-trip", "title": "Trip", "default_image": KEY}],
}
TRIPS = [
    {
        "trip": {
            "slug": "2025-trip",
            "title": "Trip",
            "default_image": KEY,
            "highlights": [],
        },
        "points": [{"image": KEY}],
    }
]


class Store:
    def __init__(self):
        self.objects = {}
        self.writes = []

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Bucket, Key])}

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[Bucket, Key] = Body
        self.writes.append((Bucket, Key))

    def copy_object(self, Bucket, Key, CopySource):
        assert CopySource["Bucket"] == "stars"
        assert CopySource["Key"].startswith("trips/photos/")
        assert CopySource["Key"].endswith(".webp")
        self.put_object(
            Bucket, Key, self.objects[CopySource["Bucket"], CopySource["Key"]]
        )


def test_export_preserves_api_json_and_only_renders_new_photos():
    store = Store()
    rendered = []

    def render(key, preset):
        rendered.append((key, preset))
        return WEBP

    first = export_trips(INDEX, TRIPS, store, render)
    assert first["photoCount"] == 1
    assert first["photoAssetCount"] == 5
    assert first["photoMode"] == "snapshot"
    assert len(rendered) == 5
    assert json.loads(store.objects["stars", "trips/index.json"]) == INDEX
    assert json.loads(store.objects["stars", "trips/2025-trip.json"]) == TRIPS[0]
    for name, key in first["files"].items():
        assert key.endswith(
            hashlib.sha256(store.objects["stars", key]).hexdigest() + ".json"
        )
    assert store.writes[-1] == ("stars", "trips/export.json")
    export_trips(INDEX, TRIPS, store, render)
    assert len(rendered) == 5
    assert all(bucket == "stars" for bucket, key in store.objects)


def test_large_exports_copy_only_derivatives_to_public_bucket():
    store = Store()
    result = export_trips(
        INDEX, TRIPS, store, lambda *_: WEBP, asset_limit=4, allow_public=True
    )
    assert result["photoMode"] == "public"
    assert result["photoBase"] == "https://factory-data.jomcgi.dev/trips/photos"
    assert (
        len([key for bucket, key in store.objects if bucket == "factory-public"]) == 5
    )


@pytest.mark.parametrize("limits", [{"asset_limit": 4}, {"byte_limit": 1}])
def test_large_exports_keep_previous_manifest_until_public_mode_is_enabled(limits):
    store = Store()
    store.objects["stars", "trips/export.json"] = b"previous"
    with pytest.raises(RuntimeError, match="public publication is disabled"):
        export_trips(INDEX, TRIPS, store, lambda *_: WEBP, **limits)
    assert store.objects["stars", "trips/export.json"] == b"previous"
    assert len(store.writes) == 5
    assert all(
        bucket == "stars" and key.startswith("trips/photos/")
        for bucket, key in store.writes
    )


def test_failed_render_leaves_previous_committed_manifest():
    store = Store()
    store.objects["stars", "trips/export.json"] = b"previous"
    with pytest.raises(ValueError, match="WebP"):
        export_trips(INDEX, TRIPS, store, lambda *_: b"JPEG original")
    assert store.objects["stars", "trips/export.json"] == b"previous"
    assert not store.writes


@pytest.mark.parametrize("tag", [b"EXIF", b"XMP "])
def test_metadata_is_rejected(tag):
    body = WEBP + tag + (2).to_bytes(4, "little") + b"xx"
    body = body[:4] + (len(body) - 8).to_bytes(4, "little") + body[8:]
    with pytest.raises(ValueError, match="metadata"):
        validate_webp(body)


@pytest.mark.parametrize(
    "key", ["../original.jpg", "img_a.jpg", "https://example.org/x.jpg"]
)
def test_signer_cannot_request_other_sources(key):
    with pytest.raises(ValueError):
        signed_path(key, "full", "00", "01")


def test_signer_matches_hub_hmac_format():
    import base64
    import hmac

    path = f"/display/plain/s3://monolith-trips/{KEY}"
    signature = (
        base64.urlsafe_b64encode(
            hmac.digest(
                bytes.fromhex("ab" * 32),
                bytes.fromhex("cd" * 32) + path.encode(),
                "sha256",
            )
        )
        .decode()
        .rstrip("=")
    )
    assert signed_path(KEY, "display", "ab" * 32, "cd" * 32) == f"/{signature}{path}"


def test_daily_export_advances_asof_even_for_unchanged_content():
    store = Store()
    result = export_trips(
        INDEX,
        TRIPS,
        store,
        lambda *_: WEBP,
        now=datetime(2026, 11, 17, tzinfo=timezone.utc),
    )
    assert result["asOf"] == "2026-11-17T00:00:00+00:00"
    assert result["refreshSeconds"] == 86400


def test_large_bytes_use_public_bucket_even_below_file_limit():
    store = Store()
    result = export_trips(
        INDEX, TRIPS, store, lambda *_: WEBP, byte_limit=1, allow_public=True
    )
    assert result["photoMode"] == "public"
    assert result["photoBytes"] == 5 * len(WEBP)
    assert result["photoMaxBytes"] == len(WEBP)
