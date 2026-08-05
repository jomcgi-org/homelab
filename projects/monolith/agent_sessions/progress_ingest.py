from __future__ import annotations

import time
from threading import Lock
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from agent_sessions import store

_token_last_write: dict[str, float] = {}
_token_lock = Lock()
_MAX_TRACKED_TOKENS = 10000


class ProgressRequest(BaseModel):
    partial_text: str
    activities: Annotated[list[dict], Field(max_length=300)] | None = None


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class ContentLengthCheckMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # Bodyless methods pass through: kubelet's GET /healthz probes send no
        # Content-Length, and a 411 here would fail liveness forever.
        if request.method in ("GET", "HEAD"):
            return await call_next(request)
        content_length = request.headers.get("content-length")
        if content_length is None:
            return Response(status_code=411)
        try:
            length = int(content_length)
            if length > 262144:
                return Response(status_code=413)
        except ValueError:
            return Response(status_code=411)
        return await call_next(request)


app.add_middleware(ContentLengthCheckMiddleware)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.post("/ingest/progress", status_code=204)
def ingest_progress(
    request: ProgressRequest, authorization: str | None = Header(default=None)
) -> Response:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid bearer token")
    progress_token = authorization.removeprefix("Bearer ")
    if not progress_token:
        raise HTTPException(status_code=401, detail="Invalid bearer token")
    if len(request.partial_text.encode("utf-8")) > 262144:
        raise HTTPException(status_code=413, detail="partial_text is too large")
    with _token_lock:
        now = time.monotonic()
        last_write = _token_last_write.get(progress_token)
        if last_write is not None and (now - last_write) < 0.15:
            return Response(status_code=204)

        result = store.write_progress_sync(
            progress_token, request.partial_text, activities=request.activities
        )

        _token_last_write[progress_token] = now
        if len(_token_last_write) > _MAX_TRACKED_TOKENS:
            oldest_token = min(_token_last_write, key=_token_last_write.get)
            del _token_last_write[oldest_token]

    if result == "unknown_token":
        raise HTTPException(status_code=401, detail="Invalid bearer token")

    return Response(status_code=204)
