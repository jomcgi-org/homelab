"""Mirror main findings into issues owned by the INTSEC sync identity."""

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
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


def findings_pages(get, slug, product):
    params = dict(
        repos=REPO,
        ref="refs/heads/main",
        issue_type=product,
        dedup="true",
        page_size=3000,
        page=0,
    )
    while True:
        try:
            payload = get(f"/{slug}/findings?{urlencode(params)}")
        except HTTPError as error:
            if (
                params["page"] == 0
                and params["page_size"] == 3000
                and error.code in (400, 422)
            ):
                params["page_size"] = 100
                continue
            raise
        rows = payload["findings"]
        yield rows
        paging = payload.get("paging") or {}
        if not rows or ("next" in paging and not paging["next"]):
            return
        params["page"] += 1


def secrets_pages(get, deployment_id):
    params = {"limit": 2000}
    seen = set()
    while True:
        payload = get(f"/{deployment_id}/secrets?{urlencode(params)}")
        yield payload["findings"]
        cursor = payload.get("cursor")
        if not cursor:
            return
        if cursor in seen:
            raise ValueError("repeated Semgrep Secrets cursor")
        seen.add(cursor)
        params["cursor"] = cursor


def snapshot(get, deployment, deployment_id):
    result = {}
    for product in ("sast", "sca", "secrets"):
        pages = (
            secrets_pages(get, deployment_id)
            if product == "secrets"
            else findings_pages(get, deployment, product)
        )
        seen = set()
        for rows in pages:
            if not isinstance(rows, list):
                raise ValueError("missing Semgrep page")
            for row in rows:
                repository = row.get("repository") or {}
                if repository.get("name") != REPO or row.get("ref") not in (
                    "main",
                    "refs/heads/main",
                ):
                    continue
                finding_id = (
                    quote(row["id"], safe="")
                    if product == "secrets"
                    else int(row["id"])
                )
                if finding_id in seen:
                    raise ValueError("overlapping Semgrep pages")
                seen.add(finding_id)
                status = row.get("status", "").lower()
                if product == "secrets":
                    status = status.removeprefix("finding_status_")
                closed = (
                    status in ("fixed", "ignored")
                    or (
                        product == "secrets"
                        and status in ("removed", "provisionally_ignored")
                    )
                    or row.get("triage_state", "").lower() == "ignored"
                )
                if not closed and status not in ("open", "reopened"):
                    continue
                severity = row.get("severity", "").lower().removeprefix("severity_")
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
                key = f"{product}/{finding_id}"
                if product == "secrets":
                    path = row.get("findingPath") or "(path unavailable)"
                    evidence = {"location": path}
                    if row.get("ruleHashId"):
                        evidence["rule"] = row["ruleHashId"]
                    if severity:
                        evidence["severity"] = severity
                else:
                    location = row["location"]
                    path = location["file_path"]
                    evidence = {
                        "rule": row["rule_name"],
                        "severity": severity,
                        "location": f"{path}:{location['line']}",
                    }
                # Indented JSON stays literal Markdown even with backticks or newlines in rule names.
                # itsaplan's mention parser still scans indented code, so no literal @ is left.
                data = json.dumps(evidence).replace("@", "\\u0040")
                link = f"https://semgrep.dev/orgs/{deployment}/findings/{finding_id}"
                result[key] = (
                    closed,
                    {"repository": REPO, "file": path, "link": link},
                    f"Semgrep finding ID: {key}\n\nUntrusted finding data\n\n    {data}\n\n{link}",
                )
    return result


class DeferredFindings(Exception):
    """Findings the API refused to move; every other finding was reconciled."""

    def __init__(self, keys):
        super().__init__("deferred linked findings: " + ", ".join(keys))
        self.keys = keys


def drain(outbox, issues, alert, final=False):
    """Send each outbox alert whose issue exists, then remove its entry.

    With final, also remove entries without an issue: their finding closed
    before its POST succeeded, and a later reopen writes a new entry.
    """
    for entry in sorted(e for e in outbox.iterdir() if e.suffix != ".tmp"):
        pending = json.loads(entry.read_text())
        if pending["key"] in issues:
            alert("SemgrepNewSecret", pending["fields"])
        elif not final:
            continue
        entry.unlink()


def reconcile(findings, plan, alert, outbox):
    """Mirror findings, then notify each new secret once its issue exists.

    A new secret gets an outbox entry before its issue POST. The alert goes
    out only after the issue exists, and the entry is removed only after the
    alert is sent. A failed POST sends no alert; a failed send retries on the
    next poll, which sees the issue and skips a second POST. Pending alerts
    for existing issues go out before any issue write, so a write that keeps
    failing for another finding cannot hold them back.
    """
    deferred = []
    outbox.mkdir(parents=True, exist_ok=True)
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
            r"Semgrep finding ID: ((?:sast|sca)/[0-9]+|secrets/[A-Za-z0-9._~%-]+)\n",
            issue["description"],
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
    drain(outbox, issues, alert)
    for key, (closed, fields, description) in findings.items():
        issue = issues.get(key)
        desired = {"title": f"Semgrep {key}", "description": description}
        if issue is None:
            if not closed:
                if key.startswith("secrets/"):
                    entry = outbox / hashlib.sha256(key.encode()).hexdigest()
                    staged = entry.with_suffix(".tmp")
                    staged.write_text(json.dumps({"key": key, "fields": fields}))
                    staged.replace(entry)
                plan(
                    PROJECT + "/issues",
                    "POST",
                    {**desired, "columnId": backlog, "labelIds": [label]},
                )
                issues[key] = desired
                if key.startswith("secrets/"):
                    drain(outbox, {key}, alert)
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
    drain(outbox, issues, alert, final=True)
    if deferred:
        raise DeferredFindings(deferred)


def send_alert(name, fields):
    summary = (
        f"{fields['repository']} {fields['file']}"
        if name == "SemgrepNewSecret"
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
        (org,) = [
            org
            for org in get("")["deployments"]
            if not deployment or org["slug"] == deployment
        ]
        slug = quote(org["slug"], safe="")
        findings = snapshot(get, slug, int(org["id"]))
    except HTTPError as error:
        if error.code not in (401, 403):
            raise
        pending = auth_marker.with_suffix(".pending")
        if not auth_marker.exists():
            if not pending.exists():
                staged = pending.with_suffix(".tmp")
                staged.write_text(str(uuid4()))
                staged.replace(pending)
            alert("SemgrepAuthenticationFailed", {"outage": pending.read_text()})
            pending.replace(auth_marker)
        return
    auth_marker.with_suffix(".pending").unlink(missing_ok=True)
    auth_marker.unlink(missing_ok=True)
    reconcile(findings, plan, alert, auth_marker.parent / "new-secrets")


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
