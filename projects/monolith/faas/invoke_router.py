"""FaaS invocation router: the ``/functions/<name>`` product URL surface (Task 11).

This is the public-shaped invocation front door (mounted at the ROOT, not under
``/api``): any HTTP method on ``/functions/<name>`` or ``/functions/<name>/<subpath>``
looks up a smoke-passed (visible) function and marshals the caller's request into
an EmberVM sync submit, then relays the guest's response back verbatim.

R1 guest-contract limitation (EmberVM transport + shim, see
``projects/embervm/runtimes/python/README.md``): EmberVM ALWAYS POSTs to the guest
at exactly the workload's ``invokePath`` (default ``/invoke``). Therefore, inside
the guest:

- ``event.httpMethod`` is ALWAYS ``"POST"`` (never the caller's real method).
- ``event.path`` is ALWAYS the invokePath (never the caller's real subpath).

So a handler CANNOT read the caller's real method/subpath from
``event.httpMethod`` / ``event.path``. This router forwards them out-of-band as
guest request headers instead: a handler reads the real method from
``event.headers["X-Forwarded-Method"]``, the real subpath from
``event.headers["X-Forwarded-Path"]``, query args from
``event.queryStringParameters`` (the raw query is appended to the guest path so
the shim parses it), and the request payload from ``event.body``.

How the shaping works (from EmberVM's submit contract): the body is forwarded to
the guest VERBATIM; request headers prefixed ``X-Ember-Guest-`` are de-prefixed
and become the guest request headers (thus ``event.headers``); ``X-Ember-Guest-Path``
sets the guest request path. ``embervm_client.submit`` owns that prefixing, so
this router hands it a plain ``extra_guest_headers`` dict plus ``guest_path``.

The ``X-Ember-Guest-*`` mechanism is deliberately NOT exposed to callers: only a
curated header set (Content-Type, Accept, and the X-Forwarded-* pair) reaches the
guest. Auth and hop-by-hop headers are never forwarded.

Response mapping (EmberVM ``?wait=true`` semantics, from
``projects/embervm/control/lib/embervm/router.ex``):

- SUCCEEDED task: EmberVM returns the guest's real status/body/headers. Relayed
  as-is (429/413/403/5xx guest bodies pass through unchanged).
- Sync-wait TIMEOUT: EmberVM returns HTTP 202 ``{task_id, state}`` (an async
  fallback, NOT a guest response). Mapped to 504 for the caller.
- A read timeout reaching us before EmberVM answers (``EmberVMTimeout``): 504.
- A connect failure (``EmberVMTransportError``): 502.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response
from sqlmodel import Session

from core.db import get_session
from faas import embervm_client
from faas.repository import get_visible_function

logger = logging.getLogger(__name__)

router = APIRouter(tags=["faas-invoke"])

# The guest invoke path. MUST match workload.build_workload_spec's invoke_path
# default: EmberVM's default guest path is "/", so a submit that does not carry
# this as X-Ember-Guest-Path 404s at the shim (Task 14a gotcha).
INVOKE_PATH = "/invoke"

# Read past the workload's invocation.timeoutSeconds (30s in build_workload_spec)
# so EmberVM's own sync-wait timeout (a 202) reaches us before our read timeout
# fires. No per-function timeout column exists in the registry yet, so this is a
# single constant; revisit when the registry grows a per-function timeout.
INVOKE_READ_TIMEOUT = 35.0

_INVOKE_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


@router.api_route("/functions/{name}", methods=_INVOKE_METHODS)
@router.api_route("/functions/{name}/{subpath:path}", methods=_INVOKE_METHODS)
async def invoke_function(
    name: str,
    request: Request,
    subpath: str = "",
    session: Session = Depends(get_session),
) -> Response:
    """Marshal the caller's request into an EmberVM sync submit and relay back.

    404 if the function is unknown OR not yet smoke-passed (invisible): the two
    are deliberately indistinguishable to the caller (do not leak existence of an
    un-smoked function).
    """
    fn = get_visible_function(session, name)
    if fn is None:
        return _json(404, {"error": "function not found"})
    return await relay_to_function(name, request, subpath)


async def relay_to_function(name: str, request: Request, subpath: str) -> Response:
    """Marshal ``request`` into an EmberVM sync submit for ``name`` and relay back.

    Split out of ``invoke_function`` so the public-tier router
    (``invoke_router_public``) reuses the identical marshaling and response
    mapping and cannot drift from the private path. The caller is responsible for
    the visibility lookup BEFORE calling this (the private router admits any
    smoke-passed function; the public router admits only ``visibility=public``),
    so this function assumes the function is already authorized to serve.
    """
    body = await request.body()
    raw_query = request.url.query
    guest_path = INVOKE_PATH + (f"?{raw_query}" if raw_query else "")

    # Curated guest headers: only these reach event.headers. Never forward
    # Authorization/Cookie/hop-by-hop headers to the guest.
    extra_guest_headers: dict[str, str] = {
        "X-Forwarded-Method": request.method,
        "X-Forwarded-Path": subpath,
    }
    content_type = request.headers.get("content-type")
    if content_type:
        extra_guest_headers["Content-Type"] = content_type
    accept = request.headers.get("accept")
    if accept:
        extra_guest_headers["Accept"] = accept

    try:
        resp = await embervm_client.submit(
            name,
            body=body,
            guest_path=guest_path,
            extra_guest_headers=extra_guest_headers,
            read_timeout=INVOKE_READ_TIMEOUT,
        )
    except embervm_client.EmberVMTimeout:
        return _json(504, {"error": "function invocation timed out"})
    except embervm_client.EmberVMTransportError:
        return _json(502, {"error": "could not reach the function runtime"})

    # EmberVM 202 = sync-wait timed out (async fallback), NOT a guest response.
    if resp.status_code == 202:
        return _json(504, {"error": "function invocation timed out"})

    # Relay the guest response verbatim. EmberVM already stripped its own framing
    # headers; forward only the content-type and let Starlette recompute the
    # length/encoding (never forward content-length/content-encoding/transfer-
    # encoding, which would be wrong for the re-emitted body).
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )


def _json(status_code: int, payload: dict) -> Response:
    """A tiny JSON Response helper (avoids importing JSONResponse everywhere)."""
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status_code, content=payload)
