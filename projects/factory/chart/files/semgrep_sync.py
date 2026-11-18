"""Mirror main findings into issues owned by the INTSEC sync identity."""

import json
import os
import re
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
            params["issue_type"] = product
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
                link = f"https://semgrep.dev/orgs/{deployment}/findings/{number}"
                result[key] = (
                    closed,
                    f"Semgrep finding ID: {key}\n\nUntrusted finding data\n\n    {json.dumps(evidence)}\n\n{link}",
                )
            if len(rows) < params["page_size"]:
                break
            page += 1
    return result


def reconcile(findings, plan):
    me = plan("/me")
    if (
        not me["authenticated"]
        or not me["user"]["isAgent"]
        or me["user"]["username"] != "intsec-sync"
    ):
        raise ValueError("expected intsec-sync identity")
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
    for key, (closed, description) in findings.items():
        issue = issues.get(key)
        desired = {"title": f"Semgrep {key}", "description": description}
        if issue is None:
            if not closed:
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
            plan(
                f"/issues/{issue['id']}/factory-semgrep",
                "PATCH",
                {"finding": key, **changes},
            )


def sync(get, deployment, plan):
    reconcile(snapshot(get, deployment), plan)


if __name__ == "__main__":
    base = "https://semgrep.dev/api/v1/deployments"
    headers = {"Authorization": "Bearer " + os.environ["SEMGREP_KEY"]}
    deployment = os.environ.get("DEPLOYMENT")
    if not deployment:
        (deployment,) = [org["slug"] for org in request(base, headers)["deployments"]]
    slug = quote(deployment, safe="")

    def semgrep(path):
        return request(f"{base}/{slug}{path}", headers)

    def itsaplan(path, method="GET", body=None):
        return request(
            os.environ["PLAN_URL"] + path,
            {"x-api-key": os.environ["PLAN_KEY"]},
            method,
            body,
        )

    sync(semgrep, slug, itsaplan)
