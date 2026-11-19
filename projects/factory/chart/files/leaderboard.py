"""Publish a reviewed snapshot of factory delivery aggregates."""

import base64
import json
import math
import os
import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO = "jomcgi-org/homelab"
SNAPSHOT = "projects/jomcgi-site/web/src/factory-outcomes.json"
FIELDS = {"arm", "n", "merges", "reverts", "median_rounds", "median_minutes"}


def rate(events, total):
    if not total:
        return None
    p = events / total
    z = 1.959963984540054
    divisor = 1 + z**2 / total
    center = (p + z**2 / (2 * total)) / divisor
    radius = z * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2)) / divisor
    return dict(
        value=round(p, 5),
        low=round(max(0, center - radius), 5),
        high=round(min(1, center + radius), 5),
    )


def snapshot(rows):
    result = []
    for row in rows:
        if set(row) != FIELDS or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", row["arm"]):
            raise ValueError("invalid aggregate fields or arm")
        n, merges, reverts = (row[k] for k in ("n", "merges", "reverts"))
        if not all(type(x) is int for x in (n, merges, reverts)) or not (
            n > 0 and 0 <= reverts <= merges <= n
        ):
            raise ValueError("invalid aggregate counts")
        for key in ("median_rounds", "median_minutes"):
            x = row[key]
            if x is not None and (
                type(x) not in (int, float) or not math.isfinite(x) or x < 0
            ):
                raise ValueError("invalid median")
        result.append(
            dict(
                arm=row["arm"],
                n=n,
                merge_rate=rate(merges, n),
                revert_rate=rate(reverts, merges),
                median_rounds=row["median_rounds"],
                median_minutes=row["median_minutes"],
            )
        )
    return result


def api(url, token, method="GET", payload=None):
    req = Request(
        url,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method=method,
        data=None if payload is None else json.dumps(payload).encode(),
    )
    with urlopen(req, timeout=30) as response:
        return json.load(response)


def publish(rows):
    content = snapshot(rows)
    jwt = Path("/broker/token").read_text().strip()
    token = api(
        os.environ["BROKER_URL"] + "/github/connector-token?repository=" + REPO, jwt
    )["token"]

    def github(path, method="GET", payload=None):
        return api(
            "https://api.github.com/repos/" + REPO + path, token, method, payload
        )

    base = github("/git/ref/heads/main")["object"]["sha"]
    path = "/contents/" + SNAPSHOT

    def contents(ref):
        obj = github(path + "?ref=" + ref)
        return obj["sha"], json.loads(base64.b64decode(obj["content"]))

    snapshot_sha, published = contents(base)
    if published == content:
        return
    branch = "leaderboard/" + snapshot_sha[:16]
    try:
        github("/git/ref/heads/" + branch)
    except HTTPError as exc:
        if exc.code != 404:
            raise
        github("/git/refs", "POST", {"ref": "refs/heads/" + branch, "sha": base})
    sha, current = contents(branch)
    if current != content:
        data = (json.dumps(content, indent=2) + "\n").encode()
        github(
            path,
            "PUT",
            dict(
                branch=branch,
                sha=sha,
                message="Refresh factory outcomes",
                content=base64.b64encode(data).decode(),
            ),
        )
    if not github("/pulls?base=main&head=jomcgi-org:" + branch):
        github(
            "/pulls",
            "POST",
            dict(title="Refresh factory outcomes", head=branch, base="main"),
        )


if __name__ == "__main__":
    publish(json.loads(Path("/data/rows.json").read_text()))
