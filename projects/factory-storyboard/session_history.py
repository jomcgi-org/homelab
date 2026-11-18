"""Read a single completed kagent A2A task using its owner's session share."""

import json
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The authentication and session share belong only to this gateway.
        return None


def completed_task(agent_url, session_id, task_id, bearer, share, *, opener=None):
    if not all((session_id, task_id, bearer, share)):
        raise ValueError("A task read requires a session, task and both credentials")
    request = Request(
        agent_url,
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": task_id,
                "method": "GetTask",
                "params": {"id": task_id},
            }
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "A2A-Version": "1.0",
            "Authorization": "Bearer " + bearer,
            "X-Share-Token": share,
        },
    )
    if opener is None:
        opener = build_opener(_NoRedirect())
    with opener.open(request, timeout=30) as response:
        envelope = json.load(response)
    if (
        envelope.get("id") != task_id
        or envelope.get("jsonrpc") != "2.0"
        or "error" in envelope
    ):
        raise ValueError("kagent rejected the shared task read")
    task = envelope["result"]
    if task["id"] != task_id or task["contextId"] != session_id:
        raise ValueError("kagent returned a different task or session")
    return task if task["status"]["state"] == "TASK_STATE_COMPLETED" else None
