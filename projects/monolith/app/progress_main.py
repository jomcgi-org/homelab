"""Entrypoint for the progress-ingest listener (ADR agents/051).

Runs as its own binary and image (mirroring app/jobs_main.py) so the listener
is its own container on its own port: the EmberVM egress allowlist is
host:port granular, and this port must expose exactly one write-only ingest
route, never the main private API.
"""

from agent_sessions.progress_ingest import app

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8091, log_level="warning")
