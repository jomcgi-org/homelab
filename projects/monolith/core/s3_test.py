"""shared_client builds one client per cached endpoint, including cold races."""

import sys
from threading import Barrier, Event, Lock, Thread
from time import monotonic
from unittest.mock import patch

from core import s3


def test_shared_client_is_built_once_per_endpoint():
    s3.shared_client.cache_clear()
    try:
        with patch.object(
            s3, "path_style_client", side_effect=lambda e: object()
        ) as build:
            first = s3.shared_client("https://a.example")
            assert s3.shared_client("https://a.example") is first
            assert s3.shared_client("https://b.example") is not first
        assert build.call_count == 2
    finally:
        s3.shared_client.cache_clear()


def _overlapping_calls(build):
    """Hold construction until all eight workers are inside shared_client."""
    start = Barrier(9, timeout=5)
    constructing = Event()
    release = Event()
    results = [None] * 8
    errors = [None] * 8
    # The baseline is an lru_cache wrapper; the repair is a Python wrapper.
    call_code = getattr(s3.shared_client, "__wrapped__", s3.shared_client).__code__

    def held_build(endpoint):
        constructing.set()
        assert release.wait(10), "controller did not release construction"
        return build(endpoint)

    def call(index):
        try:
            start.wait()
            results[index] = s3.shared_client("https://cold.example")
        except Exception as exc:
            errors[index] = exc

    def inside_client(worker, frames):
        frame = frames.get(worker.ident)
        while frame is not None:
            if frame.f_code is call_code:
                return True
            frame = frame.f_back
        return False

    # Daemon workers plus bounded joins keep a broken lock from hanging pytest
    # at executor shutdown. Every ordinary path still joins every worker.
    workers = [Thread(target=call, args=(i,), daemon=True) for i in range(8)]
    try:
        with patch.object(s3, "path_style_client", side_effect=held_build):
            try:
                for worker in workers:
                    worker.start()
                start.wait()
                assert constructing.wait(5), "no caller reached construction"
                deadline = monotonic() + 5
                while True:
                    frames = sys._current_frames()
                    if all(inside_client(worker, frames) for worker in workers):
                        break
                    assert monotonic() < deadline, (
                        "callers did not overlap in shared_client"
                    )
                    release.wait(0.001)
            finally:
                release.set()
                start.abort()
                deadline = monotonic() + 5
                for worker in workers:
                    if worker.ident is not None:
                        worker.join(max(0, deadline - monotonic()))
                assert not any(worker.is_alive() for worker in workers), "stuck callers"
        return results, errors
    finally:
        release.set()


def test_shared_client_concurrent_cold_start():
    s3.shared_client.cache_clear()
    built = []

    def build(endpoint):
        client = object()
        built.append(client)
        return client

    try:
        results, errors = _overlapping_calls(build)
        assert errors == [None] * 8
        assert len(built) == 1, (
            f"constructed {len(built)} clients for one cold endpoint"
        )
        assert all(client is built[0] for client in results)
        with patch.object(s3, "path_style_client", side_effect=build):
            assert s3.shared_client("https://cold.example") is built[0]
            assert len(built) == 1
            assert s3.shared_client("https://other.example") is not built[0]
            assert len(built) == 2
    finally:
        s3.shared_client.cache_clear()


def test_shared_client_failed_cold_start_releases_waiters():
    s3.shared_client.cache_clear()
    failure = RuntimeError("fake construction failed")
    client = object()
    attempts = []
    attempts_lock = Lock()

    def build(endpoint):
        with attempts_lock:
            attempts.append(endpoint)
            if len(attempts) == 1:
                raise failure
        return client

    try:
        results, errors = _overlapping_calls(build)
        assert any(exc is failure for exc in errors)
        assert all(exc is None or exc is failure for exc in errors)
        assert all(
            result is client for result, exc in zip(results, errors) if exc is None
        )
        # Waiters may legitimately retry after the first construction fails.
        with patch.object(s3, "path_style_client", side_effect=build):
            assert s3.shared_client("https://cold.example") is client
            attempts_after_success = len(attempts)
            assert s3.shared_client("https://cold.example") is client
            assert len(attempts) == attempts_after_success
    finally:
        s3.shared_client.cache_clear()
