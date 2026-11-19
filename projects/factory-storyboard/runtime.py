"""Publish one complete snapshot. A failed source read aborts the cycle."""

import hashlib
import json
import logging
import os
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from connector_source import delivery, session_tasks, task_messages
from delivery_stats import delivery_statistics
from publisher import public_issue, public_turn, publish, redact

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


def documents(cursor, key, project, board, archive):
    labels = {entry["id"]: entry["name"] for entry in project["labels"]}
    users = [entry["userId"] for entry in project["assignees"]]
    rows, issues, sessions, summaries = [], {}, {}, []
    goal_id = key.lower()
    for item in sorted(board, key=lambda row: row["identifier"]):
        if item["projectId"] != project["project"]["id"] or item.get("archivedAt"):
            continue
        if not public_issue(
            {"project": key, "labels": [labels[i] for i in item["labelIds"]]}, {key}
        ):
            continue
        row = delivery(cursor, key, item["id"])
        if not row:
            continue
        issue_key = row["issue_key"]
        if issue_key != item["identifier"]:
            raise ValueError("connector issue key mismatch")
        state = STATES[row["state"]]
        approval = archive.get(issue_key, {})
        title = (
            redact(item["title"], users)
            if item["title"] == approval.get("title")
            else issue_key
        )
        description = item.get("description") or ""
        description = (
            redact(description, users)
            if description == approval.get("description")
            else ""
        )
        summary = dict(goalId=goal_id, title=title, state=state, issueKey=issue_key)
        summaries.append(summary)
        rows.append(
            dict(row, created_at=datetime.fromisoformat(item["createdAt"]), state=state)
        )
        timeline = [
            dict(kind="opened", at=timestamp(item["createdAt"]), text="Issue opened")
        ]
        approved = approval.get("tasks", [])
        current, confirmed = set(), set()
        if approved:
            # Fingerprints of the connector's kagent task journal, limited to
            # the owner's approvals; the publisher never reads private
            # transcript text.
            current = {
                (r["session_id"], r["task_id"])
                for r in session_tasks(cursor, key, item["id"])
            }
            confirmed = {
                (r["session_id"], r["task_id"], r["text_sha256"])
                for r in task_messages(cursor, key, item["id"])
            }
        for record in approved:
            sid = record["session"]
            task_id = record["task"]
            texts = record["assistantTexts"]
            if (sid, task_id) in current and any(
                (sid, task_id, hashlib.sha256(text.encode()).hexdigest())
                not in confirmed
                for text in texts
            ):
                # A review round has no journal, and an approval can be
                # missing or differ. Withhold this task and keep the rest.
                LOG.warning(
                    "withheld approved task %s of %s: the connector journal "
                    "does not confirm its text",
                    task_id,
                    issue_key,
                )
                continue
            public_id = "session-" + hashlib.sha256(sid.encode()).hexdigest()
            session = sessions.setdefault(
                public_id,
                dict(version=1, kind="session", id=public_id, **summary, turns=[]),
            )
            if session["issueKey"] != issue_key:
                raise ValueError("session linked to multiple issues")
            for text in texts:
                at = timestamp(record["at"])
                number = len(session["turns"]) + 1
                session["turns"].append(
                    public_turn(
                        dict(
                            number=number,
                            at=at,
                            role="assistant",
                            completed=True,
                            text=text,
                            toolCalls=[],
                        ),
                        users,
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
            title=title,
            state=state,
            description=description,
            timeline=sorted(
                timeline, key=lambda event: datetime.fromisoformat(event["at"])
            ),
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
    import boto3
    import psycopg
    from psycopg.rows import dict_row

    logging.basicConfig(format="%(levelname)s %(message)s")
    key = os.environ["PUBLIC_PROJECT"]
    project, board = read_board(key)
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as db:
        with db.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            docs = documents(
                cursor, key, project, board, json.loads(os.environ["APPROVED_HISTORY"])
            )
    publish(
        boto3.client("s3", endpoint_url=os.environ["AWS_ENDPOINT_URL"]),
        os.environ["BUCKET"],
        datetime.now(timezone.utc),
        *docs,
    )


if __name__ == "__main__":
    main()
