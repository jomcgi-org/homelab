"""Conditional GET handling preserves the caller's cache policy and payload."""

from types import MappingProxyType

import pytest
from fastapi import Request, Response

from core.http_cache import conditional_response


def _request(validator: str | None) -> Request:
    headers = [] if validator is None else [(b"if-none-match", validator.encode())]
    return Request({"type": "http", "headers": headers})


@pytest.mark.parametrize("last_modified", [None, "Wed, 21 Oct 2015 07:28:00 GMT"])
@pytest.mark.parametrize("etag", ['"snapshot"', 'W/"snapshot"', '"a", "b"', "*"])
def test_exact_match_returns_bodyless_304(etag, last_modified):
    headers = {"ETag": etag, "Cache-Control": "public, max-age=8"}
    if last_modified is not None:
        headers["Last-Modified"] = last_modified
    response = Response(b"original", status_code=202, headers={"X-Caller": "kept"})
    original_headers = dict(response.headers)

    cached = conditional_response(_request(etag), response, MappingProxyType(headers))

    assert cached is not None
    assert cached.status_code == 304
    assert cached.body == b""
    assert dict(cached.headers) == {
        key.lower(): value for key, value in headers.items()
    }
    assert response.status_code == 202
    assert response.body == b"original"
    assert dict(response.headers) == original_headers


@pytest.mark.parametrize(
    "etag, validator",
    [
        ('"snapshot"', None),
        ('"snapshot"', '"different"'),
        ('"snapshot"', 'W/"snapshot"'),
        ('W/"snapshot"', '"snapshot"'),
        ('"snapshot"', '"snapshot", "other"'),
        ('"snapshot"', "*"),
        ('"snapshot"', ' "snapshot"'),
        ('"snapshot"', '"snapshot" '),
    ],
)
@pytest.mark.parametrize("last_modified", [None, "Wed, 21 Oct 2015 07:28:00 GMT"])
def test_nonmatch_applies_all_headers_without_changing_response(
    etag, validator, last_modified
):
    headers = {"ETag": etag, "Cache-Control": "public, max-age=8", "X-Cache": "extra"}
    if last_modified is not None:
        headers["Last-Modified"] = last_modified
    response = Response(
        b"original",
        status_code=202,
        headers={"X-Caller": "kept", "Cache-Control": "old"},
    )

    assert (
        conditional_response(_request(validator), response, MappingProxyType(headers))
        is None
    )

    assert response.status_code == 202
    assert response.body == b"original"
    assert response.headers["X-Caller"] == "kept"
    assert response.headers["Content-Length"] == "8"
    for key, value in headers.items():
        assert response.headers[key] == value
