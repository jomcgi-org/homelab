"""Child roles for process_crash_test, excluded from production source globs."""

from __future__ import annotations

import asyncio
from functools import partial
import json
import os
from pathlib import Path
import socket
import sys

from fastapi import FastAPI, Request

PROMPT = "process-loss original prompt"
PARTIAL = "remote invocation still running"
ACTIVITIES = [{"tool": "read", "file_path": "test-only.txt"}]
CHILD_ENV_KEYS = {
    "PATH",
    "LC_ALL",
    "PYTHONPATH",
    "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONUTF8",
    "DATABASE_URL",
    "EMBERVM_URL",
    "OTEL_SDK_DISABLED",
    "AGENT_SESSIONS_CHANNEL_NOTIFY",
    "PGCONNECT_TIMEOUT",
    "PGCLIENTENCODING",
    "PGOPTIONS",
}


def _loopback_only(event, args):
    if event == "socket.connect":
        address = args[1]
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise RuntimeError(f"non-loopback connection attempted: {address!r}")


def _control_plane(fd):
    import httpx
    import uvicorn

    from agent_sessions.progress_ingest import app as progress_app

    app = FastAPI()
    release = asyncio.Event()
    state = {"creates": [], "invocations": [], "deletes": [], "unexpected": []}

    @app.get("/test/state")
    async def status():
        return state

    @app.post("/test/release")
    async def finish():
        release.set()
        return {"released": True}

    @app.post("/v1/workloads/claude-runtime/sessions")
    async def create(request: Request):
        assert not await request.body()
        assert request.headers.get("authorization") is None
        identity = f"test-guest-{len(state['creates']) + 1}"
        state["creates"].append(identity)
        return {
            "session_id": identity,
            "session_token": f"test-token-{identity}",
            "lineage_id": f"test-lineage-{identity}",
        }

    @app.post("/v1/sessions/{identity}/invoke")
    async def invoke(identity: str, request: Request):
        assert request.headers["authorization"] == f"Bearer test-token-{identity}"
        assert request.headers["x-ember-guest-path"] == "/shim/turn"
        payload = await request.json()
        assert "repo" not in payload
        invocation = {"identity": identity, "message": payload["message"]}
        state["invocations"].append(invocation)
        if payload["message"] == PROMPT:
            # This server and request remain alive when the observer is killed.
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                url = os.environ["EMBERVM_URL"] + "/ingest/progress"
                headers = {"Authorization": f"Bearer {payload['progress_token']}"}
                progress = await client.post(
                    url,
                    headers=headers,
                    json={"partial_text": PARTIAL, "activities": ACTIVITIES},
                )
                progress.raise_for_status()
                invocation["progress_status"] = progress.status_code
                await asyncio.wait_for(release.wait(), timeout=100)
                late = await client.post(
                    url,
                    headers=headers,
                    json={"partial_text": "late contamination", "activities": []},
                )
                invocation["late_progress_status"] = late.status_code
        result = {
            "result": "remote completed",
            "terminal_reason": "completed",
            "stop_reason": "end_turn",
            "is_error": False,
            "num_turns": 1,
            "session_id": f"test-cli-{identity}",
            "permission_denials": [],
            "usage": {},
            "total_cost_usd": 0.0,
            "duration_ms": 1,
            "activities": [],
        }
        invocation["result"] = result
        return result

    @app.delete("/v1/sessions/{identity}")
    async def destroy(identity: str):
        state["deletes"].append(identity)
        raise RuntimeError("unexpected guest destruction")

    @app.middleware("http")
    async def unexpected_requests(request: Request, call_next):
        response = await call_next(request)
        if response.status_code >= 400 and request.url.path != "/ingest/progress":
            state["unexpected"].append(
                [request.method, request.url.path, response.status_code]
            )
        return response

    app.mount("/", progress_app)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    server.run(sockets=[socket.socket(fileno=fd)])


async def _application(role, argument):
    from agent_sessions import mcp, transport
    from shared.k8s_auth import auth_headers

    # Exercise real header construction against an empty test file, never a
    # possibly mounted Kubernetes identity. The HTTP transport stays real.
    token_path = Path.cwd() / "empty-service-account-token"
    token_path.write_text("")
    transport.auth_headers = partial(auth_headers, str(token_path))
    if role == "observer":
        await mcp._execute_pending_message(int(argument))
        return

    marker = Path(argument)
    polls = 0
    original = mcp._get_all_pending_messages_sync

    def observed_poll():
        nonlocal polls
        rows = original()
        polls += 1
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(json.dumps({"pid": os.getpid(), "polls": polls}))
        temporary.replace(marker)
        return rows

    mcp._get_all_pending_messages_sync = observed_poll
    tasks = mcp.start_pending_message_sweep()
    await tasks[0]


if __name__ == "__main__":
    assert set(os.environ) <= CHILD_ENV_KEYS, "unexpected inherited child environment"
    sys.addaudithook(_loopback_only)
    role, argument = sys.argv[1:]
    if role == "control":
        _control_plane(int(argument))
    else:
        assert role in {"observer", "recovery"}
        asyncio.run(_application(role, argument))
