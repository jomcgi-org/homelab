"""Read connector facts for an already filtered itsaplan issue in one transaction."""


def delivery(cursor, project, issue_id):
    cursor.execute(
        """SELECT issue_key, state, review_rounds, pr_number, merged_at
           FROM public.storyboard_delivery
           WHERE itsaplan_project = %s AND issue_id = %s""",
        (project, issue_id),
    )
    return cursor.fetchone()


def session_messages(cursor, project, issue_id):
    # The view returns HOME only, and only completed tasks that the binding's
    # own agent ran: never the security agent's sessions.
    cursor.execute(
        """SELECT session_id, task_id, finished_at, text
           FROM public.storyboard_prose_messages
           WHERE itsaplan_project = %s AND issue_id = %s
           ORDER BY session_id, finished_at, task_id, journal_attempt, first_event""",
        (project, issue_id),
    )
    return cursor.fetchall()


def messages_view_ready(cursor):
    # The view is absent until the connector applies its migration. The
    # factory and storyboard charts roll out independently.
    cursor.execute(
        "SELECT to_regclass('public.storyboard_prose_messages') IS NOT NULL AS ready"
    )
    return cursor.fetchone()["ready"]
