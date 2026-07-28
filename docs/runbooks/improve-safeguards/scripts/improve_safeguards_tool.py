"""improve-safeguards in-pod helper. Piped over stdin into the monolith backend pod.

Subcommands (argv after ``python3 -``):
  gather --since <ISO8601>        NDJSON moderation-decision records to stdout
  fetch-decision <event_id>       message + channel context + the user's recent
                                  moderation history + reactions (JSON)
  put-eval <event_id> <b64>       base64 eval JSON as argv[3]
                                  -> s3://artifacts/safeguards-evals/<event_id>.json

A "decision" is one ``chat.moderation_event`` row: the trust ledger's record of
one observation (ADR chat/003). ``kind`` is one of ``signal`` (a heuristic hit),
``llm_intent`` (the LLM screen flagged it), ``clean_sample`` (a sampled benign
message, label 0), ``lockout`` (score crossed below the threshold), or
``pardon``/``enforcement`` (markers). Each gathered decision is enriched with the
message content it scored (join ``chat.messages`` on the Discord id), the user's
current ledger row, and two review flags:

  near_boundary            score_after landed within +/- _BOUNDARY_BAND of the
                           lockout threshold: the classifier's least-certain
                           zone, where a false call is one signal away.
  cross_lane_disagreement  the heuristic/LLM verdict (``label``) and the shadow
                           forest (``rf_score``) disagree: a positive label with
                           a low rf_score, or a clean label with a high one. The
                           ambiguous calls most worth a human read.

The audit asks, per decision: was this the RIGHT call? A ``lockout`` (a
user-visible soft-ignore) that should not have fired is a false positive and the
worst outcome; a ``clean_sample`` whose message actually reads as abuse is a
false negative. Both are in the worst set regardless of any score.

put-eval takes its payload as a base64-encoded argv, not stdin: the script
itself is piped in over stdin (there is no second stdin channel to carry the
payload once that pipe is consumed). Callers must base64-encode the eval JSON
document and pass it as the third argument.

Postgres access is read-only (SELECT only). S3 writes are limited to the
``safeguards-evals/`` key prefix in the artifacts bucket. The skill never
mutates the ledger: a label correction (pardon/relabel) is a separate,
human-run MCP action, not something this tool performs.

eval JSON contract (written by put-eval, one key per decision under
``safeguards-evals/``):

  {
    "event_id": <int>,
    "guild_id": "<discord guild id>",
    "channel_id": "<discord channel id>",
    "user_id": "<discord user id>",
    "kind": "signal" | "llm_intent" | "clean_sample" | "lockout" | ...,
    "taxonomy_version": <int>,
    "verdict": "false-positive" | "false-negative" | "correct-flag"
               | "correct-clean" | "over-penalty" | "near-miss-boundary"
               | "mislabeled-training" | "env-failure",
    "confidence": 0.0-1.0,
    "rationale": "...",
    "signals": {
      "heuristic_signals": ["override_instructions", ...],
      "delta": -25.0,
      "score_after": 75.0,
      "label": 1,
      "rf_score": 0.12,
      "near_boundary": false,
      "cross_lane_disagreement": true
    },
    "lever": "heuristic" | "llm-prompt" | "weight" | "threshold"
             | "label-correction" | null,
    "classified_at": "<ISO8601, from date -u>",
    "levers_ref": "<git SHA of chat/safeguards.py at classify time>"
  }
"""

import base64
import json
import logging
import os
import sys

from sqlalchemy import text

from core.db import get_engine
from artifact import s3

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("improve-safeguards")

_EVAL_PREFIX = "safeguards-evals/"

# Lockout threshold mirror. The ledger reads SAFEGUARDS_LOCKOUT_THRESHOLD at
# runtime (default 40); mirror that here so near_boundary tracks the live gate.
_THRESHOLD = float(os.environ.get("SAFEGUARDS_LOCKOUT_THRESHOLD", "40"))
# A score_after within this many points of the threshold is "near boundary":
# the classifier's least-certain zone, where a wrong call is one signal away.
_BOUNDARY_BAND = float(os.environ.get("SAFEGUARDS_BOUNDARY_BAND", "15"))
# Forest disagreement cutoffs: a positive label with rf below _RF_LOW, or a
# clean label with rf at/above _RF_HIGH, is a cross-lane disagreement.
_RF_LOW = 0.4
_RF_HIGH = 0.8
# How many prior moderation events to show for the user in fetch-decision.
_USER_HISTORY = 30
# Channel context slice (minutes either side of the scored message).
_CONTEXT_MINUTES = 15


# One row per moderation decision in the window, joined to the message it scored
# and the user's current ledger row. LEFT JOIN messages: enforcement/lockout
# markers and pre-message rows may not resolve a message, and a decision is
# still worth surfacing without its text.
_GATHER_SQL = """
SELECT e.id AS event_id,
       e.created_at,
       e.guild_id,
       e.channel_id,
       e.user_id,
       e.message_id,
       e.kind,
       e.signal,
       e.detail,
       e.delta,
       e.score_after,
       e.label,
       e.rf_score,
       e.rf_model_version,
       m.username,
       m.is_bot,
       LEFT(m.content, 2000) AS content,
       t.score AS trust_score,
       t.signal_count AS trust_signal_count,
       t.lockout_count AS trust_lockout_count
FROM chat.moderation_event e
LEFT JOIN chat.messages m
       ON m.discord_message_id = e.message_id
      AND m.channel_id = e.channel_id
LEFT JOIN chat.user_trust t
       ON t.guild_id = e.guild_id
      AND t.user_id = e.user_id
WHERE e.created_at >= :since
ORDER BY e.created_at, e.id
"""


def _bucket_evals():
    """List keys already written under safeguards-evals/ -> event_id set.

    One paginated listing so gather can flag decisions already eval'd (and at
    which taxonomy_version) without a per-decision HEAD.
    """
    client = s3._client()
    have = set()
    token = None
    while True:
        kwargs = {"Bucket": s3._bucket(), "Prefix": _EVAL_PREFIX}
        if token:
            kwargs["ContinuationToken"] = token
        resp = client.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            leaf = obj["Key"][len(_EVAL_PREFIX) :]
            if leaf.endswith(".json"):
                have.add(leaf[: -len(".json")])
        if not resp.get("IsTruncated"):
            return have
        token = resp.get("NextContinuationToken")


def _near_boundary(score_after):
    if score_after is None:
        return False
    return abs(float(score_after) - _THRESHOLD) <= _BOUNDARY_BAND


def _cross_lane(label, rf_score):
    if rf_score is None or label is None:
        return False
    rf = float(rf_score)
    if int(label) == 1 and rf < _RF_LOW:
        return True
    if int(label) == 0 and rf >= _RF_HIGH:
        return True
    return False


def gather(since):
    have = _bucket_evals()
    client = s3._client()
    with get_engine().connect() as conn:
        rows = conn.execute(text(_GATHER_SQL), {"since": since}).mappings()
        for row in rows:
            rec = dict(row)
            rec["created_at"] = str(rec["created_at"])
            rec["delta"] = float(rec["delta"]) if rec["delta"] is not None else None
            rec["score_after"] = (
                float(rec["score_after"]) if rec["score_after"] is not None else None
            )
            rec["rf_score"] = (
                float(rec["rf_score"]) if rec["rf_score"] is not None else None
            )
            rec["label"] = int(rec["label"]) if rec["label"] is not None else None
            rec["is_bot"] = bool(rec["is_bot"]) if rec["is_bot"] is not None else None
            rec["trust_score"] = (
                float(rec["trust_score"]) if rec["trust_score"] is not None else None
            )
            # Split the comma-joined heuristic signal names for readability.
            rec["heuristic_signals"] = (
                [s for s in str(rec["signal"]).split(",") if s]
                if rec.get("signal")
                else []
            )
            rec["near_boundary"] = _near_boundary(rec["score_after"])
            rec["cross_lane_disagreement"] = _cross_lane(rec["label"], rec["rf_score"])
            # Worst-set flags for ranking: a wrong lockout is user-visible and
            # the highest-stakes false positive; disagreements and near-boundary
            # calls are the ambiguous zone; a resource_abuse hit is new and
            # worth confirming while the pattern is young.
            rec["is_lockout"] = rec["kind"] == "lockout"
            rec["is_pardon"] = rec["kind"] == "pardon"
            rec["has_resource_abuse"] = "resource_abuse" in rec["heuristic_signals"]
            sid = str(rec["event_id"])
            rec["has_eval"] = sid in have
            rec["eval_taxonomy_version"] = None
            if rec["has_eval"]:
                try:
                    body = client.get_object(
                        Bucket=s3._bucket(), Key=f"{_EVAL_PREFIX}{sid}.json"
                    )["Body"].read()
                    rec["eval_taxonomy_version"] = json.loads(body).get(
                        "taxonomy_version"
                    )
                except Exception:
                    logger.exception("gather: reading eval for %s failed", sid)
            print(json.dumps(rec, default=str))


_FETCH_EVENT_SQL = """
SELECT id, created_at, guild_id, channel_id, user_id, message_id, kind, signal,
       detail, delta, score_after, label, rf_score, rf_model_version,
       features_json
FROM chat.moderation_event
WHERE id = :event_id
"""

# The message this decision scored, plus a slice of the channel around it so the
# deep-read sees what the user was actually doing (banter vs a genuine probe).
_FETCH_CONTEXT_SQL = """
SELECT discord_message_id, user_id, username, is_bot,
       LEFT(content, 2000) AS content, created_at
FROM chat.messages
WHERE channel_id = :channel_id
  AND created_at >= :at - make_interval(mins => :window)
  AND created_at <= :at + make_interval(mins => :window)
ORDER BY created_at, id
"""

# The user's recent ledger history: how this decision fits their trajectory (a
# lone hit on a clean user vs the tenth strike on a serial red-teamer).
_FETCH_HISTORY_SQL = """
SELECT id, created_at, kind, signal, delta, score_after, label, rf_score
FROM chat.moderation_event
WHERE guild_id = :guild_id
  AND user_id = :user_id
ORDER BY created_at DESC, id DESC
LIMIT :limit
"""

# Reactions on the scored message (a peer's reaction to a message Bosun
# flagged/ignored is a weak ground-truth signal).
_FETCH_REACTIONS_SQL = """
SELECT message_id, emoji, reactor_id, action, created_at
FROM chat.reaction_event
WHERE channel_id = :channel_id
  AND message_id = CAST(:message_id AS text)
ORDER BY created_at, id
"""

_FETCH_TRUST_SQL = """
SELECT guild_id, user_id, score, score_updated_at, signal_count, lockout_count,
       last_signal_at
FROM chat.user_trust
WHERE guild_id = :guild_id AND user_id = :user_id
"""


def fetch_decision(event_id):
    with get_engine().connect() as conn:
        ev = (
            conn.execute(text(_FETCH_EVENT_SQL), {"event_id": event_id})
            .mappings()
            .first()
        )
        if ev is None:
            print(json.dumps({"error": "no such event", "event_id": event_id}))
            sys.exit(2)
        at = ev["created_at"]
        context = (
            conn.execute(
                text(_FETCH_CONTEXT_SQL),
                {
                    "channel_id": ev["channel_id"],
                    "at": at,
                    "window": _CONTEXT_MINUTES,
                },
            )
            .mappings()
            .all()
        )
        history = (
            conn.execute(
                text(_FETCH_HISTORY_SQL),
                {
                    "guild_id": ev["guild_id"],
                    "user_id": ev["user_id"],
                    "limit": _USER_HISTORY,
                },
            )
            .mappings()
            .all()
        )
        reactions = (
            conn.execute(
                text(_FETCH_REACTIONS_SQL),
                {"channel_id": ev["channel_id"], "message_id": ev["message_id"]},
            )
            .mappings()
            .all()
        )
        trust = (
            conn.execute(
                text(_FETCH_TRUST_SQL),
                {"guild_id": ev["guild_id"], "user_id": ev["user_id"]},
            )
            .mappings()
            .first()
        )
    evd = dict(ev)
    try:
        evd["features"] = json.loads(evd.pop("features_json") or "[]")
    except (TypeError, ValueError):
        evd["features"] = []
    out = {
        "event": {k: (str(v) if k == "created_at" else v) for k, v in evd.items()},
        "threshold": _THRESHOLD,
        "near_boundary": _near_boundary(ev["score_after"]),
        "cross_lane_disagreement": _cross_lane(ev["label"], ev["rf_score"]),
        "context_minutes": _CONTEXT_MINUTES,
        "channel_context": [dict(r) for r in context],
        "user_history": [dict(r) for r in history],
        "reactions": [dict(r) for r in reactions],
        "user_trust": dict(trust) if trust else None,
    }
    print(json.dumps(out, default=str))


_EVAL_REQUIRED = {
    "event_id",
    "guild_id",
    "channel_id",
    "user_id",
    "kind",
    "taxonomy_version",
    "verdict",
    "confidence",
    "rationale",
    "signals",
    "lever",
    "classified_at",
    "levers_ref",
}


def put_eval(event_id, payload_b64):
    # event_id is an integer PK; coerce so a crafted (e.g. slash-bearing) id can
    # never build an S3 key outside the safeguards-evals/ prefix.
    try:
        event_id = int(event_id)
    except (TypeError, ValueError):
        print(json.dumps({"error": "event_id must be an integer"}))
        sys.exit(2)
    doc = json.loads(base64.b64decode(payload_b64))
    missing = _EVAL_REQUIRED - set(doc)
    if missing:
        print(json.dumps({"error": f"missing keys: {sorted(missing)}"}))
        sys.exit(2)
    if str(doc["event_id"]) != str(event_id):
        print(json.dumps({"error": "event_id mismatch"}))
        sys.exit(2)
    key = f"{_EVAL_PREFIX}{event_id}.json"
    s3._client().put_object(
        Bucket=s3._bucket(),
        Key=key,
        Body=json.dumps(doc, indent=1).encode(),
        ContentType="application/json",
    )
    print(json.dumps({"ok": True, "key": key}))


def main():
    cmd = sys.argv[1]
    if cmd == "gather":
        gather(sys.argv[sys.argv.index("--since") + 1])
    elif cmd == "fetch-decision":
        fetch_decision(sys.argv[2])
    elif cmd == "put-eval":
        put_eval(sys.argv[2], sys.argv[3])
    else:
        print(json.dumps({"error": f"unknown subcommand {cmd}"}))


if __name__ == "__main__":
    main()
