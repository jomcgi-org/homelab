"""Tests for grimoire.migration: the run layout, its manifest and references."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from datetime import UTC, datetime

import pytest

from grimoire import migration
from grimoire.models import KnowledgeChunk, SessionEvent
from shared.testing.sqlite import sqlite_session

NOW = datetime(2026, 11, 19, 12, 30, 5, tzinfo=UTC)
RUN = "migration/20261119T123005Z/"
SOURCE = {
    "books/phb/raw/output.json.gz": b"raw marker output",
    "books/phb/raw/img/_page_3_Picture_1.jpeg": b"\xff\xd8\xff chunk image",
    "books/phb/chunks/chunks.ndjson": b'{"chunk_ref": "c1"}\n',
    "campaigns/c1/handouts/" + "a" * 32 + ".png": b"\x89PNG handout",
    "a key with spaces.txt": b"spaces",
    # An earlier run. A new run never copies it.
    "migration/20261118T000000Z/grimoire.dump": b"old dump",
}


class Body:
    def __init__(self, data: bytes):
        self.data = data
        self.closed = False

    def iter_chunks(self, chunk_size: int):
        for start in range(0, len(self.data), chunk_size):
            yield self.data[start : start + chunk_size]

    def close(self):
        self.closed = True


class FakeR2:
    """The S3 calls that write_run makes, against one in-memory bucket."""

    def __init__(self, objects: dict[str, bytes], page_size: int = 2):
        self.objects = dict(objects)
        self.page_size = page_size
        self.writes: list[str] = []
        self.corrupt: set[str] = set()

    def list_objects_v2(self, Bucket, ContinuationToken=None):
        assert Bucket == "grimoire"
        keys = sorted(self.objects)
        start = int(ContinuationToken or 0)
        page = keys[start : start + self.page_size]
        more = start + self.page_size < len(keys)
        result = {"Contents": [{"Key": key} for key in page], "IsTruncated": more}
        if more:
            result["NextContinuationToken"] = str(start + self.page_size)
        return result

    def get_object(self, Bucket, Key):
        assert Bucket == "grimoire"
        return {"Body": Body(self.objects[Key])}

    def _store(self, key: str, data: bytes):
        self.writes.append(key)
        self.objects[key] = data + b"!" if key in self.corrupt else data

    def copy_object(self, Bucket, Key, CopySource):
        assert Bucket == CopySource["Bucket"] == "grimoire"
        self._store(Key, self.objects[CopySource["Key"]])

    def put_object(self, Bucket, Key, Body):
        assert Bucket == "grimoire"
        self._store(Key, Body if isinstance(Body, bytes) else Body.read())


@pytest.fixture(name="work")
def work_fixture(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    for name in migration.DUMP_FILES:
        (work / name).write_bytes(f"{name} contents".encode())
    return work


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse(manifest: bytes) -> dict[str, str]:
    lines = manifest.decode().splitlines()
    sums = {}
    for line in lines:
        digest, path = line.split("  ", 1)
        sums[path] = digest
    assert list(sums) == sorted(sums)
    return sums


def test_run_holds_the_dump_every_object_and_a_manifest_of_stored_bytes(work):
    r2 = FakeR2(SOURCE)
    summary = migration.write_run(r2, "grimoire", "migration/", work, set(), NOW)

    assert summary == migration.RunSummary(
        RUN,
        objects=5,
        object_bytes=sum(len(v) for k, v in SOURCE.items() if "migration/" not in k),
        missing=0,
    )
    sums = parse(r2.objects[RUN + "SHA256SUMS"])
    copied = {k for k in SOURCE if not k.startswith("migration/")}
    assert set(sums) == {
        *migration.DUMP_FILES,
        "missing-references.txt",
        *(f"objects/{key}" for key in copied),
    }
    for path, digest in sums.items():
        assert digest == sha256(r2.objects[RUN + path])
    for key in copied:
        assert r2.objects[RUN + "objects/" + key] == SOURCE[key]
    assert r2.objects[RUN + "missing-references.txt"] == b""
    # Earlier runs stay out of the copy, and the manifest is the last write.
    assert not any("objects/migration/" in key for key in r2.objects)
    assert r2.writes[-1] == RUN + "SHA256SUMS"
    assert r2.writes[:3] == [RUN + name for name in migration.DUMP_FILES]


def test_manifest_reads_with_sha256sum(work, tmp_path):
    tool = shutil.which("sha256sum")
    if tool is None:
        pytest.skip("sha256sum is not installed")
    r2 = FakeR2(SOURCE)
    migration.write_run(r2, "grimoire", "migration/", work, set(), NOW)
    local = tmp_path / "run"
    for key, data in r2.objects.items():
        if key.startswith(RUN):
            path = local / key[len(RUN) :]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    result = subprocess.run(
        [tool, "--check", "--strict", "SHA256SUMS"],
        cwd=local,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count(": OK\n") == len(migration.DUMP_FILES) + 6


def test_missing_references_list_what_the_copy_lacks(work):
    r2 = FakeR2(SOURCE)
    refs = {
        "s3://grimoire/books/phb/raw/img/_page_3_Picture_1.jpeg",
        "s3://grimoire/books/phb/raw/img/gone.png",
        "s3://other-bucket/books/phb/raw/img/_page_3_Picture_1.jpeg",
        "not-a-uri",
    }
    summary = migration.write_run(r2, "grimoire", "migration/", work, refs, NOW)
    assert summary.missing == 3
    assert r2.objects[RUN + "missing-references.txt"] == (
        b"not-a-uri\n"
        b"s3://grimoire/books/phb/raw/img/gone.png\n"
        b"s3://other-bucket/books/phb/raw/img/_page_3_Picture_1.jpeg\n"
    )


def test_a_stored_dump_that_differs_stops_the_run(work):
    r2 = FakeR2(SOURCE)
    r2.corrupt.add(RUN + "vector.metadata")
    with pytest.raises(RuntimeError, match="vector.metadata"):
        migration.write_run(r2, "grimoire", "migration/", work, set(), NOW)
    assert RUN + "SHA256SUMS" not in r2.objects


def test_a_copy_that_differs_is_recorded_as_stored(work):
    # The manifest describes R2's bytes. A check against it finds the bad copy.
    r2 = FakeR2(SOURCE)
    target = RUN + "objects/books/phb/chunks/chunks.ndjson"
    r2.corrupt.add(target)
    migration.write_run(r2, "grimoire", "migration/", work, set(), NOW)
    sums = parse(r2.objects[RUN + "SHA256SUMS"])
    digest = sums["objects/books/phb/chunks/chunks.ndjson"]
    assert digest == sha256(r2.objects[target])
    assert digest != sha256(SOURCE["books/phb/chunks/chunks.ndjson"])


@pytest.mark.parametrize("key", ["bad\nkey", "bad\\key", "bad\rkey"])
def test_a_key_that_sha256sum_escapes_stops_the_run(work, key):
    r2 = FakeR2({**SOURCE, key: b"x"})
    with pytest.raises(ValueError, match="cannot be written"):
        migration.write_run(r2, "grimoire", "migration/", work, set(), NOW)
    assert RUN + "SHA256SUMS" not in r2.objects


@pytest.mark.parametrize("prefix", ["", "/migration/", "migration"])
def test_the_prefix_must_be_a_directory(work, prefix):
    with pytest.raises(ValueError, match="must be a path"):
        migration.write_run(FakeR2(SOURCE), "grimoire", prefix, work, set(), NOW)


def test_references_cover_chunk_images_and_uploaded_handouts():
    with sqlite_session() as session:
        session.add(
            KnowledgeChunk(
                book_id="phb",
                chunk_ref="image-1",
                content="",
                seq=1,
                image_ref="s3://grimoire/books/phb/raw/img/a.png",
            )
        )
        session.add(
            KnowledgeChunk(book_id="phb", chunk_ref="text-1", content="t", seq=2)
        )
        upload = "campaigns/c1/handouts/" + "b" * 32 + ".png"
        bodies = [
            ("handout", {"title": "map", "image": {"source": "upload", "key": upload}}),
            ("handout", {"image": {"source": "chunk", "chunk_id": "x"}}),
            ("handout", {"title": "no image"}),
            ("narration", {"image": {"source": "upload", "key": "ignored"}}),
        ]
        for seq, (kind, body) in enumerate(bodies, start=1):
            session.add(
                SessionEvent(
                    campaign_id="c1",
                    session_id="s1",
                    seq=seq,
                    kind=kind,
                    audience="table",
                    body=body,
                )
            )
        session.commit()
        assert migration.references(session, "grimoire") == {
            "s3://grimoire/books/phb/raw/img/a.png",
            f"s3://grimoire/{upload}",
        }


def test_run_ids_sort_in_time_order():
    earlier = migration.run_id(datetime(2026, 11, 19, 9, 59, 59, tzinfo=UTC))
    later = migration.run_id(datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC))
    assert earlier == "20261119T095959Z"
    assert earlier < later
