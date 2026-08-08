"""Public-tier FaaS invocation router: ``jomcgi.dev/functions/<name>`` (Task 13).

The public counterpart of ``invoke_router.py``. It is mounted ONLY on the public
tier (monolith-public) via ``register_public``, and differs from the private
router in exactly one way: the lookup is ``get_public_function`` (smoke-passed AND
``visibility=public``), so a private (or un-smoked) function 404s on the public
origin and is never invokable there. The marshaling, EmberVM submit, and response
relay are shared with the private router via ``relay_to_function`` so the two
cannot drift.

Security posture (ADR agents/045; public-tier checklist):
- Only ``visibility=public`` functions are reachable here (the row-level filter is
  in ``get_public_function``; the ``public_reader`` grant is table-wide, so the
  filter is load-bearing, not the grant).
- Public callers invoke pre-vetted functions only; there is deliberately NO
  ``/api/functions`` ingestion surface on the public tier (registration is the
  authenticated private-tier author surface, standing decision 7).
- Public traffic is rate-limited at two layers: a gateway
  Envoy Local limit on the ``/functions/`` HTTPRoute (120/min), plus EmberVM's
  per-principal daily quota on the single ``monolith-public`` service account all
  public traffic submits as, with the function pool cap bounding concurrency.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response
from sqlmodel import Session

from core.db import get_session
from faas.invoke_router import _INVOKE_METHODS, _json, relay_to_function
from faas.repository import get_public_function

logger = logging.getLogger(__name__)

router = APIRouter(tags=["faas-invoke-public"])


@router.api_route("/functions/{name}", methods=_INVOKE_METHODS)
@router.api_route("/functions/{name}/{subpath:path}", methods=_INVOKE_METHODS)
async def invoke_public_function(
    name: str,
    request: Request,
    subpath: str = "",
    session: Session = Depends(get_session),
) -> Response:
    """Serve a public function, or 404 for an unknown/private/un-smoked name.

    The 404 is deliberately indistinguishable across "no such function", "private
    function", and "not yet smoke-passed": the public origin must not leak the
    existence of a private or un-vetted function.
    """
    fn = get_public_function(session, name)
    if fn is None:
        return _json(404, {"error": "function not found"})
    return await relay_to_function(name, request, subpath)
