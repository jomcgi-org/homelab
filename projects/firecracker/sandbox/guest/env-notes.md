Zero-egress Python execution sandbox (ADR agents/044). One-shot: each request
runs in a fresh microVM restore and nothing persists. No network access at
all. Code runs as uid 65532 with a hard wall-clock timeout; stdout, stderr,
and files created in the working directory are returned to the caller.
