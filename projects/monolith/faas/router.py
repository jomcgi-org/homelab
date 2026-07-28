"""FaaS ingestion API: register a function through a test-run gate (ADR 045, Task 10).

``POST /api/functions`` is the only code-submission surface (standing decision 7):
an authenticated author uploads a zip + manifest; the endpoint validates it,
uploads the archive, upserts the function's ``Workload`` CR, waits for it to go
``Ready``, runs one smoke invocation through EmberVM, and only then writes the
registry row (visible via ``mark_smoked``). The registry row is written LAST, so
a function that fails its smoke run never gets a URL (the ADR 045 core security
property). Re-registration is safe: because the row is written only after a green
smoke, a failed re-registration leaves the prior working row untouched and
reverts only the CR, so a live function is never destroyed or made to 404 by a
broken new upload (plan Task 10: no zero-usable-base window).

``DELETE /api/functions/{name}`` tears down the CR, the row, and the archive.

The invocation router (Task 11) is a separate PR; this file is registration only.
"""

from __future__ import annotations

import hashlib
import logging
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlmodel import Session

from core.db import get_session
from faas import embervm_client, storage, workload
from faas.repository import (
    delete_function,
    get_function,
    mark_smoked,
    upsert_function,
)
from faas.runtime import BAKED_PACKAGES, KNOWN_RUNTIMES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/functions", tags=["faas"])

# RFC1123-ish DNS label: the function name becomes a Kubernetes resource name.
_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_NAME_MAX = 63

# The zip body cap matches the EmberVM submit body cap (8 MiB).
_ZIP_MAX_BYTES = 8 * 1024 * 1024

# The workload's guest invoke path (must match build_workload_spec's default).
_INVOKE_PATH = "/invoke"

# Ready poll budget (plan Task 10: 3 minutes) and smoke read timeout (a little
# past the 30s workload requestTimeout so the daemon's own timeout reaches us).
_READY_TIMEOUT_S = 180
_SMOKE_READ_TIMEOUT = 35.0


def _parse_requirements(raw: str | None) -> list[str]:
    """Split a comma/newline-separated requirements field into top-level names."""
    if not raw:
        return []
    parts = re.split(r"[,\n]", raw)
    return [p.strip() for p in parts if p.strip()]


@router.post("", status_code=201)
async def register_function(
    request: Request,
    name: str = Form(...),
    visibility: str = Form("private"),
    runtime: str = Form("python312"),
    handler: str = Form("app.handle"),
    requirements: str | None = Form(None),
    zip: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> dict:
    """Register a function: validate, upload, build, smoke-gate, then make visible."""
    created_by = request.headers.get("Cf-Access-Authenticated-User-Email") or "api"

    # --- Validation chain (no side effects persisted before this passes) ---
    if not _NAME_RE.match(name) or len(name) > _NAME_MAX:
        raise HTTPException(
            status_code=400,
            detail=(
                "name must be a DNS-1123 label (lowercase alphanumeric and '-', "
                "starting and ending alphanumeric) of at most 63 characters"
            ),
        )
    if runtime not in KNOWN_RUNTIMES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown runtime '{runtime}'; known: {sorted(KNOWN_RUNTIMES)}",
        )
    if visibility not in {"private", "public"}:
        raise HTTPException(
            status_code=400, detail="visibility must be 'private' or 'public'"
        )

    declared = _parse_requirements(requirements)
    missing = [pkg for pkg in declared if pkg not in BAKED_PACKAGES]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                "declared requirements not in the runtime's baked set: "
                f"{sorted(missing)}. Baked packages: {sorted(BAKED_PACKAGES)}"
            ),
        )

    zipbytes = await zip.read()
    if len(zipbytes) > _ZIP_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"zip exceeds the {_ZIP_MAX_BYTES} byte (8 MiB) cap",
        )
    sha256 = hashlib.sha256(zipbytes).hexdigest()

    # Idempotent no-op (plan Task 12, "idempotent by zip sha"): if this exact
    # archive AND config is already registered and visible (smoked), do nothing.
    # A CI-invoked re-registration on an unchanged app.py must not re-upload,
    # re-upsert the CR, or re-run a smoke (which restores a VM). The comparison
    # includes visibility/handler/runtime so a genuine change (e.g. Task 13
    # flipping og-image to public) still takes the full path and re-gates.
    existing = get_function(session, name)
    if (
        existing is not None
        and existing.zip_sha256 == sha256
        and existing.last_smoke_at is not None
        and existing.handler == handler
        and existing.runtime == runtime
        and existing.visibility == visibility
    ):
        return {
            "name": name,
            "visibility": visibility,
            "runtime": runtime,
            "handler": handler,
            "zip_sha256": sha256,
            "ready": True,
            "unchanged": True,
        }

    # Name conflict is an authorized last-write-wins overwrite (standing decision
    # 6): the authenticated author is replacing their own function. Capture the
    # prior row's CR-relevant values FIRST so a failed re-registration can revert
    # to the still-working prior version instead of destroying it. The registry
    # row is written only after the new zip passes its smoke (below), so on any
    # earlier failure the prior row stays visible untouched and we revert only the
    # Workload CR. EmberVM's no-gap turnover keeps the old base serving throughout,
    # so a re-registration never opens a zero-usable-base window (plan Task 10).
    # ``existing`` was fetched above for the idempotency check; reuse it.
    prior = existing
    prior_cr = (
        {
            "code_uri": prior.code_uri,
            "sha256": prior.zip_sha256,
            "handler": prior.handler,
            "runtime": prior.runtime,
        }
        if prior is not None
        else None
    )

    # --- Orchestrate (the registry row is written LAST, only after a green smoke) ---
    storage.put_archive(name, sha256, zipbytes)
    uri = storage.code_uri(name, sha256)

    spec = workload.build_workload_spec(
        code_uri=uri, sha256=sha256, handler=handler, runtime=runtime
    )
    await workload.upsert_workload(name, spec)

    ok, msg = await workload.wait_ready(name, timeout_s=_READY_TIMEOUT_S)
    if not ok:
        await _rollback(session, name, sha256, prior_cr)
        raise HTTPException(
            status_code=502, detail=f"function did not become ready: {msg}"
        )

    resp = await _smoke(name)
    if resp is None or not (200 <= resp.status_code < 300):
        detail = resp.text[:500] if resp is not None else "smoke transport failure"
        await _rollback(session, name, sha256, prior_cr)
        raise HTTPException(
            status_code=502,
            detail={"error": "smoke invocation failed", "detail": detail},
        )

    # Smoke passed: NOW write/overwrite the registry row and make it visible. On a
    # re-registration this is the point the prior row is replaced; on any earlier
    # failure the prior row was left intact and serving.
    upsert_function(
        session,
        name=name,
        visibility=visibility,
        runtime=runtime,
        handler=handler,
        zip_sha256=sha256,
        code_uri=uri,
        created_by=created_by,
    )
    mark_smoked(session, name)
    return {
        "name": name,
        "visibility": visibility,
        "runtime": runtime,
        "handler": handler,
        "zip_sha256": sha256,
        "ready": True,
    }


async def _smoke(name: str):
    """Run the smoke invocation, retrying ONCE on a transport-class failure only.

    A guest response (any status) is returned as-is; a 4xx/5xx guest smoke
    response is a real failure and never retried (import errors never retry, per
    the plan's open-risks row). Only a ConnectError/timeout is retried once.
    Returns the ``httpx.Response`` or ``None`` if both transport attempts failed.
    """
    for attempt in range(2):
        try:
            return await embervm_client.submit(
                name,
                body=b"{}",
                guest_path=_INVOKE_PATH,
                extra_guest_headers={"Content-Type": "application/json"},
                read_timeout=_SMOKE_READ_TIMEOUT,
            )
        except embervm_client.EmberVMTransportError as exc:
            logger.warning(
                "smoke transport failure for %s (attempt %d): %s",
                name,
                attempt + 1,
                exc,
            )
    return None


async def _rollback(
    session: Session, name: str, sha256: str, prior_cr: dict | None
) -> None:
    """Undo a failed registration WITHOUT destroying a prior working version.

    The registry row is written only after a green smoke, so on failure there is
    no new row to remove and no prior row is ever deleted here. If this was a
    re-registration (``prior_cr`` set), revert the Workload CR to the prior zip so
    the previously-working function keeps serving (EmberVM kept its base under the
    no-gap turnover property) and the prior row stays visible. If this was a first
    registration (``prior_cr`` None), tear the CR down. The newly-uploaded archive
    is deleted either way, unless the restored prior CR still references it (an
    identical-content re-registration where the new sha equals the prior sha).
    """
    if prior_cr is not None:
        try:
            await workload.upsert_workload(
                name,
                workload.build_workload_spec(
                    code_uri=prior_cr["code_uri"],
                    sha256=prior_cr["sha256"],
                    handler=prior_cr["handler"],
                    runtime=prior_cr["runtime"],
                ),
            )
        except Exception:  # noqa: BLE001: rollback must not mask the original failure
            logger.warning(
                "rollback: could not restore prior CR for %s", name, exc_info=True
            )
    else:
        try:
            await workload.delete_workload(name)
        except Exception:  # noqa: BLE001: rollback must not mask the original failure
            logger.warning(
                "rollback: delete_workload failed for %s", name, exc_info=True
            )

    if prior_cr is None or prior_cr["sha256"] != sha256:
        try:
            storage.delete_archive(name, sha256)
        except Exception:  # noqa: BLE001: leaked archive object is harmless
            logger.debug("rollback: delete_archive failed for %s", name, exc_info=True)


@router.delete("/{name}", status_code=204)
async def deregister_function(
    name: str,
    session: Session = Depends(get_session),
) -> None:
    """Delete a function: tear down its CR, registry row, and archive.

    404 if the function did not exist. The CR and archive deletions are
    best-effort; the row deletion is the authoritative removal.
    """
    existing = get_function(session, name)
    if existing is None:
        raise HTTPException(status_code=404, detail="function not found")

    try:
        await workload.delete_workload(name)
    except Exception:  # noqa: BLE001: best-effort CR teardown
        logger.warning("delete: delete_workload failed for %s", name, exc_info=True)
    delete_function(session, name)
    try:
        storage.delete_archive(name, existing.zip_sha256)
    except Exception:  # noqa: BLE001: best-effort archive teardown
        logger.debug("delete: delete_archive failed for %s", name, exc_info=True)
