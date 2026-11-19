"""Mirror main findings into issues owned by the INTSEC sync identity."""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

PROJECT = "/projects/INTSEC"
REPO = "jomcgi-org/homelab"


def request(url, headers, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode()
    req = Request(
        url,
        data=data,
        headers={**headers, "Content-Type": "application/json"},
        method=method,
    )
    with urlopen(req, timeout=30) as response:
        return json.load(response)


def snapshot(get, deployment):
    result = {}
    for product in ("sast", "sca", "secrets"):
        endpoint = "secrets" if product == "secrets" else "findings"
        params = {"repos": REPO, "ref": "refs/heads/main", "page_size": 100}
        if product != "secrets":
            params.update(issue_type=product, dedup="true", page_size=3000)
        seen = set()
        page = 0
        while True:
            params["page"] = page
            payload = get(f"/{endpoint}?{urlencode(params)}")
            rows = payload["secrets"] if "secrets" in payload else payload["findings"]
            if not isinstance(rows, list):
                raise ValueError("missing Semgrep page")
            for row in rows:
                number = int(row["id"])
                if number in seen:
                    raise ValueError("overlapping Semgrep pages")
                seen.add(number)
                if row["repository"]["name"] != REPO or row["ref"] not in (
                    "main",
                    "refs/heads/main",
                ):
                    continue
                status = row["status"].lower()
                closed = (
                    status in ("fixed", "ignored")
                    or row.get("triage_state", "").lower() == "ignored"
                )
                if not closed and status not in ("open", "reopened"):
                    continue
                severity = row["severity"].lower()
                qualifies = (
                    product == "secrets"
                    or (product == "sast" and severity in ("high", "critical"))
                    or (
                        product == "sca"
                        and str(
                            row.get("exposure", row.get("reachability", ""))
                        ).lower()
                        == "reachable"
                    )
                )
                if not closed and not qualifies:
                    continue
                key = f"{product}/{number}"
                location = row["location"]
                evidence = {
                    "rule": row["rule_name"],
                    "severity": severity,
                    "location": f"{location['file_path']}:{location['line']}",
                }
                # Indented JSON stays literal Markdown even with backticks or newlines in rule names.
                # itsaplan's mention parser still scans indented code, so no literal @ is left.
                data = json.dumps(evidence).replace("@", "\\u0040")
                link = f"https://semgrep.dev/orgs/{deployment}/findings/{number}"
                result[key] = (
                    closed,
                    {"repository": REPO, "file": location["file_path"], "link": link},
                    f"Semgrep finding ID: {key}\n\nUntrusted finding data\n\n    {data}\n\n{link}",
                )
            if len(rows) < params["page_size"]:
                break
            page += 1
    return result


class DeferredFindings(Exception):
    """Findings the API refused to move; every other finding was reconciled."""

    def __init__(self, keys):
        super().__init__("deferred linked findings: " + ", ".join(keys))
        self.keys = keys


def reconcile(findings, plan, alert):
    deferred = []
    me = plan("/me")
    if (
        not me["authenticated"]
        or not me["user"]["isAgent"]
        or me["user"]["username"] != "factory-semgrep-sync"
    ):
        raise ValueError("expected factory-semgrep-sync identity")
    setup = plan(PROJECT)
    (label,) = [l["id"] for l in setup["labels"] if l["name"] == "semgrep"]
    columns = {col["id"]: col["stateType"] for col in setup["columns"]}
    backlog = next(i for i, state in columns.items() if state == "backlog")
    done = next(i for i, state in columns.items() if state == "completed")
    issues = {}
    board = plan(PROJECT + "/issues/board")["issues"]
    archived = plan(PROJECT + "/issues/archived")
    for issue in board + archived:
        match = re.match(
            r"Semgrep finding ID: ((?:sast|sca|secrets)/[0-9]+)\n", issue["description"]
        )
        if label not in issue["labelIds"] or not match:
            continue
        path = f"/issues/{issue['id']}/feed?filter=history&order=asc&limit=1"
        entries = plan(path)["items"]
        if (
            entries
            and entries[0]["action"] == "created"
            and entries[0]["actorUserId"] == me["user"]["id"]
        ):
            key = match[1]
            if key in issues:
                raise ValueError("duplicate owned finding")
            issues[key] = issue
    for key, (closed, fields, description) in findings.items():
        issue = issues.get(key)
        desired = {"title": f"Semgrep {key}", "description": description}
        if issue is None:
            if not closed:
                if key.startswith("secrets/"):
                    alert("SemgrepNewSecret", fields)
                plan(
                    PROJECT + "/issues",
                    "POST",
                    {**desired, "columnId": backlog, "labelIds": [label]},
                )
            continue
        if issue.get("archivedAt"):
            if closed:
                continue
        terminal = columns[issue["columnId"]] in ("completed", "canceled")
        if closed != terminal:
            desired["columnId"] = done if closed else backlog
        changes = {k: v for k, v in desired.items() if issue[k] != v}
        if issue.get("archivedAt"):
            changes["restore"] = True
        if changes:
            try:
                plan(
                    f"/issues/{issue['id']}/factory-semgrep",
                    "PATCH",
                    {"finding": key, **changes},
                )
            except HTTPError as error:
                # 409: the finding is linked to another issue, which the API
                # refuses to move. Report it and keep reconciling the rest.
                if error.code != 409:
                    raise
                deferred.append(key)
    if deferred:
        raise DeferredFindings(deferred)


def send_alert(name, fields):
    summary = (
        f"{fields['repository']} {fields['file']}"
        if fields
        else "Semgrep sync authentication failed"
    )
    alert = {
        "labels": {"alertname": name, **fields},
        "annotations": {"summary": summary, "description": fields.get("link", "")},
        "endsAt": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
    }
    req = Request(
        "http://alertmanager.prometheus.svc:9093/api/v2/alerts",
        data=json.dumps([alert]).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=30):
        pass


def sync(get, deployment, plan, alert, auth_marker):
    try:
        if not deployment:
            (deployment,) = [org["slug"] for org in get("")["deployments"]]
        slug = quote(deployment, safe="")
        findings = snapshot(lambda path: get(f"/{slug}{path}"), slug)
    except HTTPError as error:
        if error.code not in (401, 403):
            raise
        if not auth_marker.exists():
            alert("SemgrepAuthenticationFailed", {})
            auth_marker.touch()
        return
    auth_marker.unlink(missing_ok=True)
    reconcile(findings, plan, alert)


if __name__ == "__main__":
    base = "https://semgrep.dev/api/v1/deployments"
    headers = {"Authorization": "Bearer " + os.environ["SEMGREP_KEY"]}

    def semgrep(path):
        return request(base + path, headers)

    def itsaplan(path, method="GET", body=None):
        return request(
            os.environ["PLAN_URL"] + path,
            {"x-api-key": os.environ["PLAN_KEY"]},
            method,
            body,
        )

    sync(
        semgrep,
        os.environ.get("DEPLOYMENT"),
        itsaplan,
        send_alert,
        Path("/state/auth-failed"),
    )
