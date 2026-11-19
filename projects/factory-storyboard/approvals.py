"""Print SQL that records the owner's approved messages in the connector store.

The storyboard_task_messages view returns a digest to the publisher only for
a row in storyboard_approved_message. The publisher login cannot read or
write that table. The owner pipes APPROVED_HISTORY through this script into
psql in the factory_connector database. The output replaces every approval
for the project, so a withdrawn message also loses its approval.
"""

import hashlib
import json
import sys


def literal(value):
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("approval fields must be text")
    return "'" + value.replace("'", "''") + "'"


def statements(project, history):
    rows = sorted(
        {
            (
                issue_key,
                task["session"],
                task["task"],
                hashlib.sha256(text.encode()).hexdigest(),
            )
            for issue_key, approval in history.items()
            for task in approval.get("tasks", [])
            for text in task["assistantTexts"]
        }
    )
    yield "SET standard_conforming_strings = on;"
    yield "BEGIN;"
    yield f"DELETE FROM public.storyboard_approved_message WHERE itsaplan_project = {literal(project)};"
    for issue_key, session, task, digest in rows:
        values = ", ".join(
            literal(v) for v in (project, issue_key, session, task, digest)
        )
        yield (
            "INSERT INTO public.storyboard_approved_message "
            "(itsaplan_project, issue_key, session_id, task_id, text_sha256) "
            f"VALUES ({values});"
        )
    yield "COMMIT;"


if __name__ == "__main__":
    for line in statements(sys.argv[1], json.load(sys.stdin)):
        print(line)
