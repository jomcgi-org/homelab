"""Write-only durable result capture on the existing guest callback listener."""

import logging

from fastapi import APIRouter, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from agent_sessions import result_receipts

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/ingest/results/{receipt_id}")
async def ingest_result(
    receipt_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid_receipt_token")
    # Content-Length is only an early rejection. Bound the actual stream too,
    # including a peer which understates its length or uses chunked framing.
    parts = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > result_receipts.MAX_RESULT_BYTES:
            raise HTTPException(status_code=413, detail="result_too_large")
        parts.append(chunk)
    try:
        return await run_in_threadpool(
            result_receipts.capture_result,
            receipt_id,
            authorization.removeprefix("Bearer "),
            b"".join(parts),
        )
    except result_receipts.ReceiptRejected as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    except Exception as exc:
        # SQL driver errors may include bound native result bytes. Report only
        # the exception class; the caller has no durable acknowledgment.
        logger.error("Result receipt capture failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="receipt_store_unavailable"
        ) from None
