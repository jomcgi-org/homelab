"""improve-ambient in-pod helper. Piped over stdin into the monolith backend pod.

Subcommands (argv after ``python3 -``):
  gather --since <ISO8601>       NDJSON episode records to stdout
  fetch-episode <episode_id>     transcript slice + reactions + agent result (JSON)
  put-eval <episode_id> <b64>    base64 eval JSON as argv[3] -> s3://artifacts/ambient-evals/<episode_id>.json

An "episode" is one ambient activation: a ``chat.attention_decision`` row with
``decision='engage'``. Each is enriched with its trigger message, the reactions
and human follow-up that landed in the same channel shortly after the engage,
and (best effort) the agent thread that ran.

put-eval takes its payload as a base64-encoded argv, not stdin: the script
itself is piped in over stdin (there is no second stdin channel to carry the
payload once that pipe is consumed). Callers must base64-encode the eval JSON
document and pass it as the third argument.

Postgres access is read-only (SELECT only). S3 writes are limited to the
``ambient-evals/`` key prefix in the artifacts bucket.

Reaction attribution is EXACT when the engage's reply id is known, and a
temporal-window heuristic otherwise. ``chat.attention_decision.reply_message_id``
(populated when Bosun replies to an ambient engage) is the reply's Discord id,
and reactions attach to that reply, so when it is present reactions are matched
exactly on ``reaction_event.message_id = reply_message_id`` (no window needed).
When it is null (the agent thread-opening path, or rows from before that column
was populated) reactions fall back to a channel plus time-after-engage window
(``_WINDOW_MINUTES``). Human follow-up is always attributed by the window (there
is no exact "did a human follow up" link). Overlapping episodes in the same
channel can share the windowed signal; the Opus deep-read (fetch-episode)
resolves ambiguity per episode. ``reaction_match`` on each record flags which
path was used ("exact" or "time-window-heuristic").

Each record also carries ``withheld_reason``: why the engage produced no
in-channel reply, disambiguating the silent paths that a null
``reply_message_id`` alone cannot. One of ``agent_thread`` (routed to an agent
session), ``no_reply`` (the model chose silence), ``send_gate`` (the
post-generation gate vetoed the drafted reply), ``empty_reply`` (no content),
``locked_out`` (the author is trust-locked-out, so a would-be engage was
suppressed to a brig emoji; ADR chat/003), or null when a reply was sent.
Records from before the column existed are null
even when suppressed, so scope any rate over it to the current window.
The agent-session match is likewise a nearest-in-time heuristic
(``_AGENT_WINDOW_MINUTES``): ``agent_sessions.agent_sessions`` carries the
Discord thread but no parent-channel or trigger-message key, so an episode is
tied to the session created closest in time around the engage
(a tight window after, with 2 min of pre-engage slack for clock skew).

eval JSON contract (written by put-eval, one key per episode under
``ambient-evals/``):

  {
    "episode_id": <int>,
    "channel_id": "<discord channel id>",
    "author_id": "<trigger author discord id>",
    "taxonomy_version": <int>,
    "failure_modes": [{"mode": "...", "confidence": 0.0, "rationale": "..."}],
    "signals": {
      "attention_confidence": 0.0,
      "became_agent": true,
      "human_followups": 0,
      "same_author_replied": false,
      "reactions": {"emoji": count, ...},
      "net_reaction": 0
    },
    "route": "chat" | "agent",
    "classified_at": "<ISO8601, from date -u>",
    "levers_ref": "<git SHA of the lever files at classify time>"
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

# stdout carries the NDJSON records, so best-effort failures log to stderr.
logger = logging.getLogger(__name__)

# Reactions and human follow-up are attributed to an episode by channel plus a
# window after the engage's created_at. An ambient reply lands within seconds of
# the engage (or after a slower agent run), and humans react/reply while the
# exchange is still live; 30 minutes captures that live span while dropping
# stale reactions on the same channel much later. Env-overridable.
_WINDOW_MINUTES = int(os.environ.get("AMBIENT_WINDOW_MINUTES", "30"))

# The agent-thread ledger row is created inside the agent flow, shortly after
# the engage (queue ack + orchestrator compile can add up to a minute). A tight
# window keeps the nearest-in-time match honest; a wide one would grab an
# unrelated concurrent run. A small negative slack absorbs clock skew.
_AGENT_WINDOW_MINUTES = int(os.environ.get("AMBIENT_AGENT_WINDOW_MINUTES", "5"))

# S3 writes are confined to this prefix in the artifacts bucket.
_EVAL_PREFIX = "ambient-evals/"

_GATHER_SQL = """
SELECT
    ad.id                       AS episode_id,
    ad.channel_id               AS channel_id,
    ad.message_id               AS trigger_message_id,
    ad.confidence               AS attention_confidence,
    ad.directive_version        AS directive_version,
    ad.created_at               AS created_at,
    ad.reply_message_id         AS reply_message_id,
    ad.withheld_reason          AS withheld_reason,
    m.user_id                   AS author_id,
    m.username                  AS author_name,
    LEFT(m.content, 500)        AS trigger_content,
    m.is_bot                    AS trigger_is_bot,
    at.session_id               AS agent_session_id,
    at.status                   AS agent_state,
    at.model                    AS agent_model,
    LEFT(at.result, 400)        AS agent_result_head,
    at.terminal_reason          AS agent_terminal_reason,
    at.created_at               AS agent_created_at,
    rx.reaction_counts          AS reaction_counts,
    rx.net_reaction             AS net_reaction,
    rx.reaction_total           AS reaction_total,
    fu.human_followups          AS human_followups,
    fu.same_author_replied      AS same_author_replied
FROM chat.attention_decision ad
LEFT JOIN chat.messages m
       ON m.discord_message_id = ad.message_id
-- Nearest-in-time agent session around the engage. See module docstring.
LEFT JOIN LATERAL (
    SELECT s.local_session_id AS session_id, s.status, s.model,
           turn.result_text AS result, turn.terminal_reason, s.created_at
    FROM agent_sessions.agent_sessions s
    LEFT JOIN LATERAL (
        SELECT t.result_text, t.terminal_reason
        FROM agent_sessions.agent_turns t
        WHERE t.session_id = s.id
        ORDER BY t.seq DESC
        LIMIT 1
    ) turn ON TRUE
    WHERE s.created_at >= ad.created_at - make_interval(mins => 2)
      AND s.created_at <= ad.created_at + make_interval(mins => :agent_window)
    ORDER BY ABS(EXTRACT(EPOCH FROM (s.created_at - ad.created_at)))
    LIMIT 1
) at ON TRUE
-- Reactions on Bosun's reply, aggregated to per-emoji add counts and an overall
-- net (adds minus removes). EXACT match on the reply id when known
-- (reply_message_id), else a channel + time-after-engage window fallback.
LEFT JOIN LATERAL (
    SELECT
        COALESCE(json_object_agg(e.emoji, e.adds), '{}') AS reaction_counts,
        COALESCE(SUM(e.net), 0)                          AS net_reaction,
        COALESCE(SUM(e.adds), 0)                         AS reaction_total
    FROM (
        SELECT re.emoji,
               SUM(CASE WHEN re.action = 'add' THEN 1 ELSE 0 END)  AS adds,
               SUM(CASE WHEN re.action = 'add' THEN 1 ELSE -1 END) AS net
        FROM chat.reaction_event re
        WHERE re.channel_id = ad.channel_id
          AND (
              (ad.reply_message_id IS NOT NULL
                   AND re.message_id = ad.reply_message_id)
           OR (ad.reply_message_id IS NULL
                   AND re.created_at >= ad.created_at
                   AND re.created_at <= ad.created_at + make_interval(mins => :window))
          )
        GROUP BY re.emoji
    ) e
) rx ON TRUE
-- Human follow-up in the same channel within the window, and whether the same
-- author who triggered the engage came back.
LEFT JOIN LATERAL (
    SELECT
        COUNT(*)                            AS human_followups,
        BOOL_OR(fm.user_id = m.user_id)     AS same_author_replied
    FROM chat.messages fm
    WHERE fm.channel_id = ad.channel_id
      AND fm.is_bot = FALSE
      AND fm.created_at > ad.created_at
      AND fm.created_at <= ad.created_at + make_interval(mins => :window)
      AND fm.discord_message_id <> ad.message_id
) fu ON TRUE
WHERE ad.decision = 'engage'
  AND ad.created_at >= :since
ORDER BY ad.created_at
"""


def _bucket_evals():
    """List keys already written under ambient-evals/ -> episode_id set.

    One paginated listing so gather can flag episodes already eval'd (and at
    which taxonomy_version) without a per-episode HEAD.
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


def gather(since):
    have = _bucket_evals()
    client = s3._client()
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(_GATHER_SQL),
            {
                "since": since,
                "window": _WINDOW_MINUTES,
                "agent_window": _AGENT_WINDOW_MINUTES,
            },
        ).mappings()
        for row in rows:
            rec = dict(row)
            rec["created_at"] = str(rec["created_at"])
            rec["agent_created_at"] = (
                str(rec["agent_created_at"]) if rec["agent_created_at"] else None
            )
            rec["attention_confidence"] = (
                float(rec["attention_confidence"])
                if rec["attention_confidence"] is not None
                else None
            )
            rec["net_reaction"] = int(rec["net_reaction"] or 0)
            rec["reaction_total"] = int(rec["reaction_total"] or 0)
            rec["human_followups"] = int(rec["human_followups"] or 0)
            rec["same_author_replied"] = bool(rec["same_author_replied"])
            # reaction_counts arrives as a JSON string on some drivers.
            if isinstance(rec["reaction_counts"], str):
                rec["reaction_counts"] = json.loads(rec["reaction_counts"])
            rec["became_agent"] = rec["agent_session_id"] is not None
            rec["agent_match"] = (
                "time-window-heuristic" if rec["became_agent"] else None
            )
            rec["reaction_match"] = (
                "exact" if rec.get("reply_message_id") else "time-window-heuristic"
            )
            sid = str(rec["episode_id"])
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


_FETCH_EPISODE_SQL = """
SELECT id, channel_id, message_id, confidence, directive_version, created_at,
       reply_message_id
FROM chat.attention_decision
WHERE id = :episode_id
"""

# A generous slice around the engage so the deep-read sees the run-up and the
# reply/aftermath. One window either side keeps the transcript readable while
# covering a slower agent reply.
_FETCH_MESSAGES_SQL = """
SELECT discord_message_id, user_id, username, is_bot,
       LEFT(content, 2000) AS content, thinking IS NOT NULL AS has_thinking,
       created_at
FROM chat.messages
WHERE channel_id = :channel_id
  AND created_at >= :engage_at - make_interval(mins => :window)
  AND created_at <= :engage_at + make_interval(mins => :window)
ORDER BY created_at, id
"""

# EXACT match on the reply id when known, else the channel + window fallback.
# The nullable :reply_message_id bind is cast to text so psycopg can type the
# NULL in the IS NULL / IS NOT NULL branches (repo gotcha). Use CAST(... AS text)
# rather than the `::text` spelling: SQLAlchemy's text() lexer refuses to bind a
# param immediately followed by `::`, leaving `:reply_message_id::text` literal
# and tripping a Postgres "syntax error at or near :".
_FETCH_REACTIONS_SQL = """
SELECT message_id, emoji, reactor_id, action, created_at
FROM chat.reaction_event
WHERE channel_id = :channel_id
  AND (
      (CAST(:reply_message_id AS text) IS NOT NULL
           AND message_id = CAST(:reply_message_id AS text))
   OR (CAST(:reply_message_id AS text) IS NULL
           AND created_at >= :engage_at
           AND created_at <= :engage_at + make_interval(mins => :window))
  )
ORDER BY created_at, id
"""

_FETCH_AGENT_SQL = """
SELECT s.local_session_id AS session_id, s.status, s.model, s.repo,
       turn.prompt, turn.result_text, turn.terminal_reason, s.created_at,
       s.last_turn_at
FROM agent_sessions.agent_sessions s
LEFT JOIN LATERAL (
    SELECT t.prompt, t.result_text, t.terminal_reason
    FROM agent_sessions.agent_turns t
    WHERE t.session_id = s.id
    ORDER BY t.seq DESC
    LIMIT 1
) turn ON TRUE
WHERE s.created_at >= :engage_at - make_interval(mins => 2)
  AND s.created_at <= :engage_at + make_interval(mins => :agent_window)
ORDER BY ABS(EXTRACT(EPOCH FROM (s.created_at - :engage_at)))
LIMIT 1
"""


def fetch_episode(episode_id):
    with get_engine().connect() as conn:
        ep = (
            conn.execute(text(_FETCH_EPISODE_SQL), {"episode_id": episode_id})
            .mappings()
            .first()
        )
        if ep is None:
            print(json.dumps({"error": "no such episode", "episode_id": episode_id}))
            sys.exit(2)
        engage_at = ep["created_at"]
        slice_params = {
            "channel_id": ep["channel_id"],
            "engage_at": engage_at,
            "window": _WINDOW_MINUTES,
        }
        msgs = conn.execute(text(_FETCH_MESSAGES_SQL), slice_params).mappings().all()
        reactions = (
            conn.execute(
                text(_FETCH_REACTIONS_SQL),
                {**slice_params, "reply_message_id": ep["reply_message_id"]},
            )
            .mappings()
            .all()
        )
        agent = (
            conn.execute(
                text(_FETCH_AGENT_SQL),
                {"engage_at": engage_at, "agent_window": _AGENT_WINDOW_MINUTES},
            )
            .mappings()
            .first()
        )
    out = {
        "episode_id": ep["id"],
        "channel_id": ep["channel_id"],
        "trigger_message_id": ep["message_id"],
        "reply_message_id": ep["reply_message_id"],
        "reaction_match": (
            "exact" if ep["reply_message_id"] else "time-window-heuristic"
        ),
        "attention_confidence": (
            float(ep["confidence"]) if ep["confidence"] is not None else None
        ),
        "directive_version": ep["directive_version"],
        "engage_at": str(engage_at),
        "window_minutes": _WINDOW_MINUTES,
        "messages": [dict(r) for r in msgs],
        "reactions": [dict(r) for r in reactions],
        "agent_thread": dict(agent) if agent else None,
    }
    print(json.dumps(out, default=str))


_EVAL_REQUIRED = {
    "episode_id",
    "channel_id",
    "author_id",
    "taxonomy_version",
    "failure_modes",
    "signals",
    "route",
    "classified_at",
    "levers_ref",
}


def put_eval(episode_id, payload_b64):
    # episode_id is an integer PK; coerce so a crafted (e.g. slash-bearing) id
    # can never build an S3 key outside the ambient-evals/ prefix.
    try:
        episode_id = int(episode_id)
    except (TypeError, ValueError):
        print(json.dumps({"error": "episode_id must be an integer"}))
        sys.exit(2)
    doc = json.loads(base64.b64decode(payload_b64))
    missing = _EVAL_REQUIRED - set(doc)
    if missing:
        print(json.dumps({"error": f"missing keys: {sorted(missing)}"}))
        sys.exit(2)
    if str(doc["episode_id"]) != str(episode_id):
        print(json.dumps({"error": "episode_id mismatch"}))
        sys.exit(2)
    key = f"{_EVAL_PREFIX}{episode_id}.json"
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
    elif cmd == "fetch-episode":
        fetch_episode(sys.argv[2])
    elif cmd == "put-eval":
        put_eval(sys.argv[2], sys.argv[3])
    else:
        print(json.dumps({"error": f"unknown subcommand {cmd}"}))
        sys.exit(2)


if __name__ == "__main__":
    main()
