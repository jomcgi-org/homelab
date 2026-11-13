"""Mechanical conditional GET handling for precomputed cache headers."""

from collections.abc import Mapping

from fastapi import Request, Response


def conditional_response(
    request: Request, response: Response, headers: Mapping[str, str]
) -> Response | None:
    """Return a bodyless 304 on an exact validator match, or apply headers.

    Callers construct the ETag and cache policy after checking freshness.
    Compare validators verbatim, without parsing weak tags, lists or wildcards.
    """
    if request.headers.get("if-none-match") == headers["ETag"]:
        return Response(status_code=304, headers=headers)
    for key, value in headers.items():
        response.headers[key] = value
    return None
