"""Publication boundary: no originals/metadata, resumable renders, atomic manifest."""

import hashlib
import io
import json
from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError
from PIL import Image

from trips.export import (
    WORKERS_ASSET_BYTES,
    export_trips,
    signed_path,
    slug_key,
    validate_webp,
)

KEY = "img_" + "a" * 12 + ".jpg"


def webp(lossless=False):
    """A real WebP with one decodable frame and no metadata."""
    out = io.BytesIO()
    Image.new("RGB", (4, 3), (40, 90, 160)).save(out, "WEBP", lossless=lossless)
    return out.getvalue()


def riff(*chunks):
    """A WebP container around raw (tag, payload) chunks."""
    body = b"WEBP" + b"".join(
        tag + len(data).to_bytes(4, "little") + data + b"\0" * (len(data) % 2)
        for tag, data in chunks
    )
    return b"RIFF" + len(body).to_bytes(4, "little") + body


WEBP = webp()
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
    def __init__(self, sizes=None):
        self.objects = {}
        self.writes = []
        # Reported sizes by key, so a test can model a file too large to build.
        self.sizes = sizes or {}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": self.sizes.get(Key, len(self.objects[Bucket, Key]))}

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
    assert json.loads(store.objects["stars", "trips/trip/2025-trip.json"]) == TRIPS[0]
    files = [first["files"]["index"], *first["files"]["trips"].values()]
    assert set(first["files"]["trips"]) == {"2025-trip"}
    for key in files:
        assert key.startswith("trips/v/")
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


@pytest.mark.parametrize("lossless", [False, True])
def test_real_lossy_and_lossless_webp_pass(lossless):
    validate_webp(webp(lossless))


@pytest.mark.parametrize(
    "body",
    [
        riff((b"ALPH", b"\x00" * 4)),  # no image chunk at all
        riff((b"VP8 ", b"xx")),  # an image chunk that cannot hold a frame
        riff((b"VP8L", b"\x2f" + b"\x00" * 4)),  # a lossless header with no data
    ],
)
def test_webp_without_a_decodable_frame_is_rejected(body):
    with pytest.raises(ValueError):
        validate_webp(body)


def test_incomplete_render_leaves_previous_committed_manifest():
    store = Store()
    store.objects["stars", "trips/export.json"] = b"previous"
    with pytest.raises(ValueError):
        export_trips(INDEX, TRIPS, store, lambda *_: riff((b"VP8 ", b"xx")))
    assert store.objects["stars", "trips/export.json"] == b"previous"
    assert not store.writes


@pytest.mark.parametrize("slug", ["export", "index"])
def test_no_trip_slug_can_replace_the_manifest_or_index(slug):
    store = Store()
    trip = {"slug": slug, "title": "Trip", "default_image": KEY, "highlights": []}
    payload = {"trip": trip, "points": []}
    index = {"count": 1, "trips": [{"slug": slug, "default_image": KEY}]}
    result = export_trips(index, [payload], store, lambda *_: WEBP)
    manifest = json.loads(store.objects["stars", "trips/export.json"])
    assert manifest["files"] == result["files"]
    assert json.loads(store.objects["stars", "trips/index.json"]) == index
    assert json.loads(store.objects["stars", f"trips/trip/{slug}.json"]) == payload
    assert result["files"]["trips"][slug].startswith(f"trips/v/trip/{slug}-")


@pytest.mark.parametrize(
    "key",
    [
        "../original.jpg",
        "img_../x.jpg",
        "img_a/b.jpg",
        "img_.jpg",
        "img_a.jpg?x=1",
        "img_a.png",
        "https://example.org/x.jpg",
    ],
)
def test_signer_cannot_request_other_sources(key):
    with pytest.raises(ValueError):
        signed_path(key, "full", "00", "01")


# Key shapes on the live trip: the hub's ingest writes the first 12 hex digits
# of the SHA-256, and one older photo is img_000038.jpg.
@pytest.mark.parametrize("key", ["img_2d30f3c65619.jpg", "img_000038.jpg"])
def test_signer_accepts_the_hubs_image_keys(key):
    assert signed_path(key, "thumb", "00", "01").endswith(
        f"/thumb/plain/s3://monolith-trips/{key}"
    )


@pytest.mark.parametrize("preset", ["original", "raw", "", "full/../x"])
def test_signer_rejects_unsupported_presets(preset):
    with pytest.raises(ValueError):
        signed_path(KEY, preset, "00", "01")


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


def test_one_file_over_the_workers_asset_limit_needs_public_mode():
    assert WORKERS_ASSET_BYTES == 25 * 1024**2
    big = f"trips/photos/{KEY}/full.webp"
    store = Store(sizes={big: WORKERS_ASSET_BYTES + 1})
    store.objects["stars", "trips/export.json"] = b"previous"
    with pytest.raises(RuntimeError, match="public publication is disabled"):
        export_trips(INDEX, TRIPS, store, lambda *_: WEBP)
    assert store.objects["stars", "trips/export.json"] == b"previous"

    result = export_trips(INDEX, TRIPS, store, lambda *_: WEBP, allow_public=True)
    assert result["photoMode"] == "public"
    assert result["photoMaxBytes"] == WORKERS_ASSET_BYTES + 1


def test_a_file_at_the_workers_asset_limit_stays_in_the_snapshot():
    big = f"trips/photos/{KEY}/full.webp"
    store = Store(sizes={big: WORKERS_ASSET_BYTES})
    result = export_trips(
        INDEX, TRIPS, store, lambda *_: WEBP, byte_limit=2 * WORKERS_ASSET_BYTES
    )
    assert result["photoMode"] == "snapshot"
    assert result["photoBase"] == "/data/trips/photos"


@pytest.mark.parametrize(
    "slug", ["summer_2025", "Trip.Name", "..", "a/b", "a%2Fb", "naïve trip"]
)
def test_any_api_slug_exports_under_its_own_key(slug):
    store = Store()
    trip = {"slug": slug, "title": "Trip", "default_image": KEY, "highlights": []}
    payload = {"trip": trip, "points": []}
    index = {"count": 1, "trips": [{"slug": slug, "default_image": KEY}]}
    result = export_trips(index, [payload], store, lambda *_: WEBP)
    alias = f"trips/trip/{slug_key(slug)}.json"
    assert json.loads(store.objects["stars", alias]) == payload
    assert set(result["files"]["trips"]) == {slug}
    assert "/" not in slug_key(slug) and slug_key(slug) not in (".", "..")


def test_slug_keys_never_collide():
    slugs = ["a/b", "a%2Fb", "a.b", "a%2Eb", "A", "a", "a~b", "a%7Eb", "summer_2025"]
    assert len({slug_key(s) for s in slugs}) == len(slugs)


@pytest.mark.parametrize("slug", ["", None, 7])
def test_a_trip_without_a_usable_slug_is_rejected(slug):
    with pytest.raises(ValueError, match="slug"):
        slug_key(slug)
