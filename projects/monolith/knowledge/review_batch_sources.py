"""A request cache shared only within one bounded review-batch preparation."""

from __future__ import annotations

import json
import re
import time

import httpx
from core.github import GITHUB_API, GITHUB_REPO, _github_headers

from knowledge import review_coverage as coverage
from knowledge.review_sources import (
    EvidenceUnavailable,
    EvidenceUnsupported,
    collect_sources,
)

MAX_REQUESTS = 64
MAX_CACHE_BYTES = 512_000
DEADLINE_SECONDS = 90


class BatchSources:
    def __init__(self, *, fetch=None, repo=GITHUB_REPO):
        self.fetch = fetch
        self.repo = repo
        self.client = None
        self.cache = {}
        self.requests = 0
        self.cache_hits = 0
        self.bytes = 0
        self.transfer_bytes = 0
        self.pin_bytes = 0
        self.started = time.monotonic()
        self.stopped = None

    def __enter__(self):
        if self.fetch is None:
            self.client = httpx.Client(timeout=10, follow_redirects=False)
        return self

    def __exit__(self, *_):
        if self.client is not None:
            self.client.close()

    def read(self, path, *, max_bytes=coverage.MAX_RESPONSE_BYTES):
        if time.monotonic() - self.started >= DEADLINE_SECONDS:
            self.stopped = "batch source deadline exhausted"
        if self.stopped:
            raise EvidenceUnavailable(self.stopped)
        if path in self.cache:
            self.cache_hits += 1
            return self.cache[path]
        if self.requests >= MAX_REQUESTS:
            self.stopped = "batch source request budget exhausted"
            raise EvidenceUnavailable(self.stopped)
        self.requests += 1
        try:
            if self.fetch is not None:
                result = self.fetch(path)
            elif coverage.enabled():
                result = coverage.read_response(
                    self.client,
                    path,
                    deadline_at=self.started + DEADLINE_SECONDS,
                    charge=self._charge,
                    max_bytes=max_bytes,
                )
            else:
                response = self.client.get(GITHUB_API + path, headers=_github_headers())
                if response.status_code != 200:
                    message = f"GitHub returned {response.status_code}"
                    if (
                        response.status_code in {401, 403, 429}
                        or response.status_code >= 500
                    ):
                        self.stopped = message
                    raise EvidenceUnavailable(message)
                result = response.json()
        except httpx.HTTPError as exc:
            message = "batch source transport unavailable"
            if not coverage.enabled():
                self.stopped = message
            raise EvidenceUnavailable(message) from exc
        if not isinstance(result, dict):
            raise EvidenceUnavailable("malformed GitHub response")
        size = (
            coverage.serialized_size(result)
            if coverage.enabled()
            else len(json.dumps(result).encode())
        )
        if coverage.enabled():
            if self.fetch is not None:
                self._charge(size)
            if coverage.response_size(result) > min(
                max_bytes, coverage.MAX_RESPONSE_BYTES
            ):
                raise EvidenceUnsupported("source response byte bound exceeded")
        if coverage.enabled() and time.monotonic() - self.started >= DEADLINE_SECONDS:
            self.stopped = "batch source deadline exhausted"
            raise EvidenceUnavailable(self.stopped)
        if self.bytes + size > MAX_CACHE_BYTES:
            message = "batch source byte budget exhausted"
            if not coverage.enabled():
                self.stopped = message
            raise EvidenceUnavailable(message)
        self.bytes += size
        self.cache[path] = result
        return result

    def _charge(self, size):
        self.transfer_bytes += size
        if self.transfer_bytes > MAX_CACHE_BYTES:
            self.stopped = "batch source transport byte budget exhausted"
            raise EvidenceUnavailable(self.stopped)

    def pin(self):
        result = self.read(f"/repos/{self.repo}/git/ref/heads/main")
        self.pin_bytes = coverage.response_size(result) if coverage.enabled() else 0
        sha = result.get("object", {}).get("sha")
        if not re.fullmatch(r"[0-9a-f]{40}", str(sha)):
            raise EvidenceUnavailable("missing main commit")
        return sha

    def collect(self, claim, revision):
        return collect_sources(
            claim,
            fetch=self.read,
            bounded_fetch=self.read,
            initial_bytes=self.pin_bytes,
            repo=self.repo,
            main_sha=revision,
        )[0]

    def metrics(self):
        return {
            "source_requests": self.requests,
            "source_cache_hits": self.cache_hits,
            "source_response_bytes": self.bytes,
            "collection_seconds": round(time.monotonic() - self.started, 3),
        }
