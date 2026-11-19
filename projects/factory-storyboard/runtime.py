"""Publish one complete snapshot. A failed source read aborts the cycle."""

import hashlib
import json
import logging
import os
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from connector_source import delivery, messages_view_ready, session_messages
from delivery_stats import delivery_statistics
from publisher import gitleaks, public_issue, public_texts, public_turns, publish

LOG = logging.getLogger(__name__)

STATES = {
    "active": "Working",
    "blocked": "Stopped",
    "review": "In review",
    "merging": "In merge queue",
    "done": "Shipped",
    "stopped": "Stopped",
}


def timestamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        raise ValueError("source timestamp has no timezone")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The itsaplan key belongs only to the configured API.
        return None


def read_board(key):
    base = os.environ["ITSAPLAN_URL"] + "/projects/" + quote(key, safe="")

    def get(path):
        req = Request(base + path, headers={"x-api-key": os.environ["ITSAPLAN_KEY"]})
        with build_opener(_NoRedirect()).open(req, timeout=30) as response:
            return json.load(response)

    project = get("")
    if project["project"]["key"] != key:
        raise ValueError("unexpected project")
    grant = project["permissions"]["work_items"]
    if grant.get("read") is not True or any(
        enabled for action, enabled in grant.items() if action != "read"
    ):
        raise ValueError("publisher requires a read-only itsaplan identity")
    return project, get("/issues/board")["issues"]


def documents(cursor, key, project, board, scan=gitleaks):
    """Return the snapshot documents, or None if the connector view is absent."""
    if not messages_view_ready(cursor):
        # Fail closed: before the connector migration applies, publish
        # nothing and keep the last manifest.
        LOG.error(
            "connector view storyboard_messages is absent: the connector "
            "migration has not applied; this cycle published nothing"
        )
        return None
    labels = {entry["id"]: entry["name"] for entry in project["labels"]}
    users = [entry["userId"] for entry in project["assignees"]]
    goal_id = key.lower()
    eligible = []
    for item in sorted(board, key=lambda row: row["identifier"]):
        if item["projectId"] != project["project"]["id"] or item.get("archivedAt"):
            continue
        # The store holds no labels. A private or security issue stops here,
        # before any connector read, so a newly labelled issue stops
        # publishing on the next cycle.
        if not public_issue(
            {"project": key, "labels": [labels[i] for i in item["labelIds"]]}, {key}
        ):
            continue
        row = delivery(cursor, key, item["id"])
        if not row:
            continue
        if row["issue_key"] != item["identifier"]:
            raise ValueError("connector issue key mismatch")
        eligible.append((item, row, session_messages(cursor, key, item["id"])))
    # One scan covers every title and description.
    texts = public_texts(
        [
            text
            for item, _, _ in eligible
            for text in (item["title"], item.get("description") or "")
        ],
        users,
        scan,
    )
    rows, issues, sessions, summaries = [], {}, {}, []
    for index, (item, row, messages) in enumerate(eligible):
        issue_key = row["issue_key"]
        state = STATES[row["state"]]
        title, description = texts[2 * index : 2 * index + 2]
        if title is None or description is None:
            LOG.warning("withheld issue text of %s: secret scan", issue_key)
        summary = dict(
            goalId=goal_id, title=title or issue_key, state=state, issueKey=issue_key
        )
        summaries.append(summary)
        rows.append(
            dict(row, created_at=datetime.fromisoformat(item["createdAt"]), state=state)
        )
        timeline = [
            dict(kind="opened", at=timestamp(item["createdAt"]), text="Issue opened")
        ]
        # The view's rows: the text of each assistant message in a completed
        # task of this issue, in session and task order.
        for message in messages:
            sid = message["session_id"]
            public_id = "session-" + hashlib.sha256(sid.encode()).hexdigest()
            session = sessions.setdefault(
                public_id,
                dict(version=1, kind="session", id=public_id, **summary, turns=[]),
            )
            if session["issueKey"] != issue_key:
                raise ValueError("session linked to multiple issues")
            at = timestamp(message["finished_at"])
            number = len(session["turns"]) + 1
            session["turns"].append(
                dict(
                    number=number,
                    at=at,
                    role="assistant",
                    text=message["text"],
                    toolCalls=[],
                )
            )
            timeline.append(
                dict(
                    kind="session",
                    at=at,
                    turn=number,
                    sessionId=public_id,
                    text="Session turn",
                )
            )
        if row["merged_at"]:
            timeline.append(
                dict(
                    kind="merged",
                    at=timestamp(row["merged_at"]),
                    prNumber=row["pr_number"],
                    text="Pull request merged",
                )
            )
        issues[issue_key] = dict(
            version=1,
            kind="issue",
            key=issue_key,
            goalId=goal_id,
            title=summary["title"],
            state=state,
            description=description or "",
            timeline=sorted(
                timeline, key=lambda event: datetime.fromisoformat(event["at"])
            ),
        )
    # One scan covers every turn. A matching turn keeps only its header.
    public = iter(
        public_turns(
            [turn for session in sessions.values() for turn in session["turns"]],
            users,
            scan,
        )
    )
    for session in sessions.values():
        session["turns"] = [next(public) for _ in session["turns"]]
        for turn in session["turns"]:
            if turn["withheld"]:
                LOG.warning(
                    "withheld turn %d of %s in %s: secret scan",
                    turn["number"],
                    session["id"],
                    session["issueKey"],
                )
    stats, charts = delivery_statistics(rows)
    shipped = sorted(
        (r for r in rows if r["merged_at"]), key=lambda r: r["merged_at"], reverse=True
    )
    by_key = {s["issueKey"]: s for s in summaries}
    goal = dict(
        goalId=goal_id,
        title=key,
        chapter=[],
        state="Shipped" if rows and len(shipped) == len(rows) else "Working",
        shipped=len(shipped),
        total=len(rows),
    )
    opened_by_day = Counter(timestamp(r["created_at"])[:10] for r in rows)
    merged_by_day = Counter(timestamp(r["merged_at"])[:10] for r in shipped)
    burn_up = []
    total = completed = 0
    for day in sorted(opened_by_day.keys() | merged_by_day.keys()):
        total += opened_by_day[day]
        completed += merged_by_day[day]
        burn_up.append(dict(date=day, shipped=completed, total=total))
    goals = {
        goal_id: dict(
            version=1,
            kind="goal",
            id=goal_id,
            title=key,
            state=goal["state"],
            chapter=[],
            issueKeys=list(issues),
            burnUp=burn_up,
        )
    }
    story = dict(
        version=1,
        kind="story",
        headline="Factory delivery",
        notice="",
        narrative=[],
        stats=stats,
        charts=charts,
        goals=[goal],
        recentlyShipped=[by_key[r["issue_key"]] for r in shipped],
        workInProgress=[
            s for s in summaries if s["state"] not in ("Stopped", "Shipped")
        ],
    )
    return story, goals, issues, sessions


def main():
    logging.basicConfig(format="%(levelname)s %(message)s")
    if not os.environ.get("ITSAPLAN_KEY"):
        # The factory chart's PostSync bootstrap writes the key Secret. A
        # cycle that runs first publishes nothing and keeps the last manifest.
        LOG.warning("itsaplan key not bootstrapped yet; skipping this cycle")
        return
    import boto3
    import psycopg
    from psycopg.rows import dict_row

    key = os.environ["PUBLIC_PROJECT"]
    project, board = read_board(key)
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as db:
        with db.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            docs = documents(cursor, key, project, board)
    if docs is None:
        return
    publish(
        boto3.client("s3", endpoint_url=os.environ["AWS_ENDPOINT_URL"]),
        os.environ["BUCKET"],
        datetime.now(timezone.utc),
        *docs,
    )


if __name__ == "__main__":
    main()
