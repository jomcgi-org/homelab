"""Read connector facts for an already filtered itsaplan issue in one transaction."""


def delivery(cursor, project, issue_id):
    cursor.execute(
        """SELECT issue_key, state, review_rounds, pr_number, merged_at
           FROM public.storyboard_delivery
           WHERE itsaplan_project = %s AND issue_id = %s""",
        (project, issue_id),
    )
    return cursor.fetchone()


def session_tasks(cursor, project, issue_id):
    cursor.execute(
        """SELECT session_id, kagent_namespace, kagent_agent, task_id
           FROM public.storyboard_tasks
           WHERE itsaplan_project = %s AND issue_id = %s
           ORDER BY session_id, task_id""",
        (project, issue_id),
    )
    return cursor.fetchall()


def task_messages(cursor, project, issue_id):
    # SHA-256 digests only: the view never returns transcript text.
    cursor.execute(
        """SELECT session_id, task_id, text_sha256
           FROM public.storyboard_task_messages
           WHERE itsaplan_project = %s AND issue_id = %s""",
        (project, issue_id),
    )
    return cursor.fetchall()
