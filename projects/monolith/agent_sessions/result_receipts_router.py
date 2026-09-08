"""Write-only durable result capture on the existing guest callback listener."""

import asyncio
import logging
from threading import BoundedSemaphore

from fastapi import APIRouter, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from agent_sessions import result_receipts

router = APIRouter()
logger = logging.getLogger(__name__)
# This listener shares a 256 MiB sidecar with progress reporting. Reject excess
# uploads instead of queuing their native bodies in memory. Capture is optional;
# a busy callback leaves the normal synchronous guest response available.
_capture_slots = BoundedSemaphore(1)
BODY_TIMEOUT_SECONDS = 10


@router.post("/ingest/results/{receipt_id}")
async def ingest_result(
    receipt_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid_receipt_token")
    if not _capture_slots.acquire(blocking=False):
        raise HTTPException(status_code=503, detail="receipt_receiver_busy")
    try:
        token = authorization.removeprefix("Bearer ")
        await run_in_threadpool(result_receipts.authenticate_receipt, receipt_id, token)
        # Content-Length is only an early rejection. Bound the actual stream too,
        # including a peer which understates its length or uses chunked framing.
        parts = []
        total = 0
        async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
            async for chunk in request.stream():
                total += len(chunk)
                if total > result_receipts.MAX_RESULT_BYTES:
                    raise HTTPException(status_code=413, detail="result_too_large")
                parts.append(chunk)
        return await run_in_threadpool(
            result_receipts.capture_result,
            receipt_id,
            token,
            b"".join(parts),
        )
    except HTTPException:
        raise
    except TimeoutError:
        raise HTTPException(status_code=408, detail="receipt_body_timeout") from None
    except result_receipts.ReceiptRejected as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    except Exception as exc:
        # SQL driver errors may include bound native result bytes. Report only
        # the exception class; the caller has no durable acknowledgment.
        logger.error("Result receipt capture failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="receipt_store_unavailable"
        ) from None
    finally:
        _capture_slots.release()
