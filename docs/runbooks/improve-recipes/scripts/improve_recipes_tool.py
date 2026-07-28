"""improve-recipes in-pod helper. Piped over stdin into the monolith backend pod.

Subcommands (argv after ``python3 -``):
  gather --since <ISO8601>       NDJSON session records to stdout
  fetch-session <session_id>     base64 sessions.db blob to stdout
  put-eval <session_id> <b64>    base64-encoded eval JSON as argv[3] -> s3://artifacts/<sid>/eval.json

put-eval takes its payload as a base64-encoded argv, not stdin: the script
itself is piped in over stdin (there is no second stdin channel to carry the
payload once that pipe is consumed). Callers must base64-encode the eval JSON
document and pass it as the third argument.

Postgres access is read-only (SELECT only). S3 writes are limited to eval.json.
"""

import base64
import json
import logging
import sqlite3
import sys
import tempfile

from sqlalchemy import text

from core.db import get_engine
from artifact import s3
from goosecracker import sessions

# stdout carries the NDJSON records, so best-effort failures log to stderr.
logger = logging.getLogger(__name__)

# Inter-message gaps above this (seconds) are treated as idle (waiting for an
# owner reply between turns) and excluded from active_seconds; gaps at or below
# it are contiguous guest work and summed. Chosen above a slow single work step
# (a local-model turn or a slow tool call, observed up to ~75s) and well below a
# typical owner-reply pause (minutes), so a multi-turn thread's idle waits drop
# out while its real work bursts are kept.
_ACTIVE_IDLE_GAP_SECONDS = 180

_GATHER_SQL = """
SELECT at.thread_id, at.session_id, at.recipe, at.tier, at.state,
       LEFT(at.task, 500) AS task,
       LEFT(at.result, 400) AS result_head,
       at.result_error,
       at.created_at, at.completed_at,
       EXTRACT(EPOCH FROM (at.completed_at - at.created_at)) AS wall_seconds,
       gs.transcript,
       ob.orchestrator_route,
       ob.plan_step_count,
       ob.plan_latency_ms,
       ob.plan_json
FROM claude_agent.agent_threads at
LEFT JOIN chat.goosecracker_sessions gs ON gs.discord_thread = at.session_id
LEFT JOIN LATERAL (
    -- The most recent orchestrator verdict for this thread (ADR 036). Route
    -- 'goose' means the run was driven by a DeepSeek-constructed runtime plan;
    -- 'failopen' means it fell back to the baked agent router. plan_step_count
    -- and plan_json expose what DeepSeek selected/sequenced. Replan events are
    -- NOT here (kept as a log, not a telemetry row): detect those on deep-read.
    SELECT b.route AS orchestrator_route,
           (b.brief_json->>'plan_step_count') AS plan_step_count,
           (b.brief_json->>'plan_latency_ms') AS plan_latency_ms,
           b.brief_json->'plan' AS plan_json
    FROM chat.orchestrator_brief b
    WHERE b.thread_id = at.session_id
    ORDER BY b.created_at DESC
    LIMIT 1
) ob ON TRUE
WHERE at.created_at >= :since
ORDER BY at.created_at
"""


def _owner_turns(transcript):
    # _join_transcript in chat/goosecracker.py separates owner turns with a
    # blank line; a heuristic (turns containing blank lines overcount) but
    # consistent across sessions.
    if not transcript:
        return None
    return len([b for b in transcript.split("\n\n") if b.strip()])


def _active_seconds(session_id):
    """Real guest work time for a session, from goose sessions.db timestamps.

    wall_seconds (thread created_at -> completed_at) counts the whole thread
    lifetime, most of which is waiting for owner replies between turns, so it
    wildly overstates effort (a 2510s thread whose guest ran ~150s). This sums
    the gaps between consecutive goose messages, dropping any gap above
    _ACTIVE_IDLE_GAP_SECONDS (an owner-reply pause) and keeping the rest (the
    contiguous tool-call / model / tool-result cadence of an active run). Robust
    to goose recording tool results as role=user (which breaks a naive
    user->assistant turn span). Best-effort: returns None when the db is
    absent, unreadable, or has too few messages to span any time.
    """
    try:
        blob = sessions.load(session_id)
    except Exception:
        logger.exception("active_seconds: sessions.load failed for %s", session_id)
        return None
    if not blob:
        return None
    try:
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            f.write(blob)
            f.flush()
            con = sqlite3.connect(f.name)
            try:
                ts = [
                    row[0]
                    for row in con.execute(
                        "SELECT created_timestamp FROM messages "
                        "ORDER BY created_timestamp, id"
                    )
                    if row[0] is not None
                ]
            finally:
                con.close()
    except Exception:
        logger.exception(
            "active_seconds: reading sessions.db failed for %s", session_id
        )
        return None
    if len(ts) < 2:
        return None
    total = 0.0
    for earlier, later in zip(ts, ts[1:]):
        gap = later - earlier
        if 0 < gap <= _ACTIVE_IDLE_GAP_SECONDS:
            total += gap
    return total


def _bucket_index():
    """One paginated listing of the artifacts bucket -> per-session key sets."""
    client = s3._client()
    have = {}
    token = None
    while True:
        kwargs = {"Bucket": s3._bucket()}
        if token:
            kwargs["ContinuationToken"] = token
        resp = client.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            sid, _, leaf = key.partition("/")
            if leaf:
                have.setdefault(sid, set()).add(leaf)
        if not resp.get("IsTruncated"):
            return have
        token = resp.get("NextContinuationToken")


def gather(since):
    have = _bucket_index()
    client = s3._client()
    with get_engine().connect() as conn:
        rows = conn.execute(text(_GATHER_SQL), {"since": since}).mappings()
        for row in rows:
            rec = dict(row)
            sid = rec["session_id"]
            rec["owner_turns"] = _owner_turns(rec.pop("transcript"))
            rec["created_at"] = str(rec["created_at"])
            rec["completed_at"] = (
                str(rec["completed_at"]) if rec["completed_at"] else None
            )
            rec["wall_seconds"] = (
                float(rec["wall_seconds"]) if rec["wall_seconds"] is not None else None
            )
            leaves = have.get(sid, set())
            rec["has_sessions_db"] = "sessions.db" in leaves
            # Real guest work time from sessions.db message gaps (see
            # _active_seconds): rank on this, not wall_seconds, which is thread
            # lifetime inflated by owner-reply waits. Only computable when the
            # session db is present.
            rec["active_seconds"] = (
                _active_seconds(sid) if rec["has_sessions_db"] else None
            )
            rec["has_eval"] = "eval.json" in leaves
            rec["eval_taxonomy_version"] = None
            if rec["has_eval"]:
                body = client.get_object(Bucket=s3._bucket(), Key=f"{sid}/eval.json")[
                    "Body"
                ].read()
                rec["eval_taxonomy_version"] = json.loads(body).get("taxonomy_version")
            print(json.dumps(rec, default=str))


def fetch_session(session_id):
    blob = sessions.load(session_id)
    if blob is None:
        print(json.dumps({"error": "no sessions.db", "session_id": session_id}))
        sys.exit(2)
    sys.stdout.write(base64.b64encode(blob).decode())


_EVAL_REQUIRED = {
    "session_id",
    "recipe",
    "taxonomy_version",
    "failure_modes",
    "metrics",
    "classified_at",
    "recipes_ref",
}


def put_eval(session_id, payload_b64):
    doc = json.loads(base64.b64decode(payload_b64))
    missing = _EVAL_REQUIRED - set(doc)
    if missing:
        print(json.dumps({"error": f"missing keys: {sorted(missing)}"}))
        sys.exit(2)
    if doc["session_id"] != session_id:
        print(json.dumps({"error": "session_id mismatch"}))
        sys.exit(2)
    s3._client().put_object(
        Bucket=s3._bucket(),
        Key=f"{session_id}/eval.json",
        Body=json.dumps(doc, indent=1).encode(),
        ContentType="application/json",
    )
    print(json.dumps({"ok": True, "key": f"{session_id}/eval.json"}))


def main():
    cmd = sys.argv[1]
    if cmd == "gather":
        gather(sys.argv[sys.argv.index("--since") + 1])
    elif cmd == "fetch-session":
        fetch_session(sys.argv[2])
    elif cmd == "put-eval":
        put_eval(sys.argv[2], sys.argv[3])
    else:
        print(json.dumps({"error": f"unknown subcommand {cmd}"}))
        sys.exit(2)


if __name__ == "__main__":
    main()
