"""improve-artifacts in-pod helper. Piped over stdin into the monolith backend pod.

Subcommands (argv after ``python3 -``):
  gather --since <ISO8601>          NDJSON artifact records to stdout
  fetch-artifact <artifact_id>      base64 index.html to stdout
  put-eval <artifact_id> <b64>      base64-encoded design-eval JSON as argv[3]
                                    -> s3://artifacts/<id>/design-eval.json

put-eval takes its payload as a base64-encoded argv, not stdin: the script
itself is piped in over stdin (there is no second stdin channel to carry the
payload once that pipe is consumed). Callers must base64-encode the eval JSON
document and pass it as the third argument.

Postgres access is read-only (SELECT only). S3 writes are limited to
design-eval.json.
"""

import base64
import json
import sys
from datetime import datetime, timezone

from sqlalchemy import text

from core.db import get_engine
from artifact import s3

# Recipe/thread context for the artifact ids found in the bucket. Artifact ids
# are random capability tokens minted per thread (chat.goosecracker_sessions.
# artifact_id), NOT session ids, so recover the Discord thread via
# goosecracker_sessions and join agent_threads on it. DISTINCT ON keeps the
# most recent thread row when a thread ran multiple turns.
_CONTEXT_SQL = """
SELECT DISTINCT ON (gs.artifact_id)
       gs.artifact_id,
       gs.discord_thread AS session_id,
       at.recipe, at.tier, at.state,
       LEFT(at.task, 500) AS task,
       at.created_at
FROM chat.goosecracker_sessions gs
LEFT JOIN claude_agent.agent_threads at ON at.session_id = gs.discord_thread
WHERE gs.artifact_id = ANY(:ids)
ORDER BY gs.artifact_id, at.created_at DESC
"""

_EVAL_LEAF = "design-eval.json"


def _bucket_index():
    """One paginated listing -> {artifact_id: {leaf: last_modified}}."""
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
            aid, _, leaf = key.partition("/")
            if leaf:
                have.setdefault(aid, {})[leaf] = obj["LastModified"]
        if not resp.get("IsTruncated"):
            return have
        token = resp.get("NextContinuationToken")


def gather(since):
    since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
    if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=timezone.utc)
    have = _bucket_index()
    client = s3._client()
    ids = [
        aid
        for aid, leaves in have.items()
        if "index.html" in leaves and leaves["index.html"] >= since_dt
    ]
    context = {}
    if ids:
        with get_engine().connect() as conn:
            rows = conn.execute(text(_CONTEXT_SQL), {"ids": ids}).mappings()
            context = {r["artifact_id"]: dict(r) for r in rows}
    for aid in sorted(ids, key=lambda a: have[a]["index.html"], reverse=True):
        leaves = have[aid]
        ctx = context.get(aid, {})
        rec = {
            "artifact_id": aid,
            "session_id": ctx.get("session_id"),
            "published_at": str(leaves["index.html"]),
            "recipe": ctx.get("recipe"),
            "tier": ctx.get("tier"),
            "state": ctx.get("state"),
            "task": ctx.get("task"),
            "thread_created_at": str(ctx["created_at"])
            if ctx.get("created_at")
            else None,
            "has_design_eval": _EVAL_LEAF in leaves,
            "design_eval_rubric_version": None,
        }
        if rec["has_design_eval"]:
            body = client.get_object(Bucket=s3._bucket(), Key=f"{aid}/{_EVAL_LEAF}")[
                "Body"
            ].read()
            rec["design_eval_rubric_version"] = json.loads(body).get("rubric_version")
        print(json.dumps(rec, default=str))


def fetch_artifact(artifact_id):
    got = s3.get_artifact(artifact_id)
    if got is None:
        print(json.dumps({"error": "no index.html", "artifact_id": artifact_id}))
        sys.exit(2)
    html, _etag = got
    sys.stdout.write(base64.b64encode(html).decode())


_EVAL_REQUIRED = {
    "artifact_id",
    "session_id",
    "recipe",
    "rubric_version",
    "scores",
    "slop_tells",
    "defects",
    "viewports",
    "judged_at",
    "recipes_ref",
}


def put_eval(artifact_id, payload_b64):
    doc = json.loads(base64.b64decode(payload_b64))
    missing = _EVAL_REQUIRED - set(doc)
    if missing:
        print(json.dumps({"error": f"missing keys: {sorted(missing)}"}))
        sys.exit(2)
    if doc["artifact_id"] != artifact_id:
        print(json.dumps({"error": "artifact_id mismatch"}))
        sys.exit(2)
    s3._client().put_object(
        Bucket=s3._bucket(),
        Key=f"{artifact_id}/{_EVAL_LEAF}",
        Body=json.dumps(doc, indent=1).encode(),
        ContentType="application/json",
    )
    print(json.dumps({"ok": True, "key": f"{artifact_id}/{_EVAL_LEAF}"}))


def main():
    cmd = sys.argv[1]
    if cmd == "gather":
        gather(sys.argv[sys.argv.index("--since") + 1])
    elif cmd == "fetch-artifact":
        fetch_artifact(sys.argv[2])
    elif cmd == "put-eval":
        put_eval(sys.argv[2], sys.argv[3])
    else:
        print(json.dumps({"error": f"unknown subcommand {cmd}"}))
        sys.exit(2)


if __name__ == "__main__":
    main()
