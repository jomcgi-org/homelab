"""Hermetic checks for the artifact verification boundary, without Linux/KVM."""

import hashlib
import io
import urllib.error

import pytest

import run_linux


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(run_linux, "ROOT", tmp_path)
    (tmp_path / "artifacts").mkdir()
    return tmp_path / "artifacts"


def artifact(body):
    return {
        "url": "https://example.invalid/kernel",
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def test_download_checks_bytes_before_publishing(cache, monkeypatch):
    spec = artifact(b"expected binary")
    monkeypatch.setattr(
        run_linux.urllib.request,
        "urlopen",
        lambda *_a, **_kw: io.BytesIO(b"wrong binary"),
    )
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        run_linux.download(spec)
    assert list(cache.iterdir()) == []


def test_verified_cache_needs_no_network(cache, monkeypatch):
    spec = artifact(b"expected binary")
    expected = cache / spec["sha256"]
    expected.write_bytes(b"expected binary")

    def offline(*_args, **_kwargs):
        raise AssertionError("network used despite verified cache")

    monkeypatch.setattr(run_linux.urllib.request, "urlopen", offline)
    assert run_linux.download(spec) == expected


def test_corrupt_cache_is_replaced_only_by_verified_bytes(cache, monkeypatch):
    spec = artifact(b"expected binary")
    expected = cache / spec["sha256"]
    expected.write_bytes(b"corruption")
    monkeypatch.setattr(
        run_linux.urllib.request,
        "urlopen",
        lambda *_a, **_kw: io.BytesIO(b"expected binary"),
    )
    assert run_linux.download(spec).read_bytes() == b"expected binary"
    assert list(cache.iterdir()) == [expected]


def test_interrupted_download_removes_partial_file(cache, monkeypatch):
    class Interrupted(io.BytesIO):
        def read(self, *_args):
            raise urllib.error.URLError("connection lost")

    monkeypatch.setattr(
        run_linux.urllib.request, "urlopen", lambda *_a, **_kw: Interrupted()
    )
    with pytest.raises(urllib.error.URLError):
        run_linux.download(artifact(b"expected binary"))
    assert list(cache.iterdir()) == []
