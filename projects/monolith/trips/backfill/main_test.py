"""Unit tests for the backfill image-copy step (fake S3, never the network)."""

from botocore.exceptions import ClientError

from trips.backfill import main


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
