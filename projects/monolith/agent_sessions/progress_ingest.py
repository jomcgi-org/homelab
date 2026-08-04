from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel
from sqlmodel import Session, select

from agent_sessions import store
from agent_sessions.models import AgentSession
from core.db import get_engine


class ProgressRequest(BaseModel):
    partial_text: str


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


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

    with Session(get_engine()) as session:
        known = session.exec(
            select(AgentSession.id).where(AgentSession.progress_token == progress_token)
        ).first()
    if known is None:
        raise HTTPException(status_code=401, detail="Invalid bearer token")

    store.write_progress_sync(progress_token, request.partial_text)
    return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8091)
