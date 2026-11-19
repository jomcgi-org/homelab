"""Publish factory health as static data (HOME-260).

Each run checks the factory cluster and the jomcgi.dev Worker, then writes
health.json to the Garage bucket factory-health. The factory-public mirror
copies only that key to R2 with a 60-second cache, so public readers never
call the cluster. The document names each component and its up or down
state only: no versions, error text, hostnames or secrets.
"""

import datetime
import hashlib
import hmac
import json
import os
import ssl
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

SERVICE_ACCOUNT = "/var/run/secrets/kubernetes.io/serviceaccount"
APPLICATION = "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/argocd"
CLOUDFLARE = "https://api.cloudflare.com/client/v4"
WORKER = "jomcgi-site"
KEY = "health.json"
# Clock skew allowed for a reconciledAt time slightly ahead of this node.
SKEW = 300


def clock():
    return datetime.datetime.now(datetime.timezone.utc)


def parse(timestamp):
    return datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def kube(path):
    with open(SERVICE_ACCOUNT + "/token") as token:
        request = Request(
            "https://kubernetes.default.svc" + path,
            headers={"Authorization": "Bearer " + token.read().strip()},
        )
    context = ssl.create_default_context(cafile=SERVICE_ACCOUNT + "/ca.crt")
    with urlopen(request, context=context, timeout=10) as response:
        return json.load(response)


def cloudflare(path, env):
    request = Request(
        CLOUDFLARE + path,
        headers={"Authorization": "Bearer " + env["CLOUDFLARE_API_TOKEN"]},
    )
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def cluster(now, env):
    # Argo CD's own Application is Healthy only while its controller, repo
    # server and API run, and reconciledAt shows it reconciled in 15 minutes.
    status = kube(APPLICATION)["status"]
    age = (now - parse(status["reconciledAt"])).total_seconds()
    return status["health"]["status"] == "Healthy" and -SKEW <= age <= 900


def worker(now, env):
    # The Worker's latest deployment must have a Cloudflare deployment ID and
    # a creation time no later than the moment its response arrived, so a
    # deployment made during this run still counts.
    account = quote(env["CLOUDFLARE_ACCOUNT_ID"], safe="")
    body = cloudflare(f"/accounts/{account}/workers/scripts/{WORKER}/deployments", env)
    checked = clock()
    deployments = body["result"]["deployments"] if body.get("success") else []
    if not deployments:
        return False
    latest = max(deployments, key=lambda d: parse(d["created_on"]))
    created = parse(latest["created_on"])
    return bool(latest["id"]) and created <= checked


CHECKS = {"factory-cluster": cluster, "jomcgi-site-worker": worker}
# The HOME-415 alert check (README "Factory health"): ALERT_TEST=true adds
# this component, always down.
TEST_COMPONENT = "alert-test"


def compose(results, now):
    """Return the public document: component names and states only."""
    components = [
        {"name": name, "state": "up" if ok else "down"}
        for name, ok in sorted(results.items())
    ]
    healthy = all(c["state"] == "up" for c in components)
    return {
        "status": "healthy" if healthy else "unhealthy",
        "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "components": components,
    }


def signature(method, url, headers, payload_hash, secret, region, amz_date):
    """Return (scope, signed headers, signature) for AWS Signature Version 4."""
    parts = urlsplit(url)
    names = sorted(headers)
    canonical = "\n".join(
        [
            method,
            parts.path,
            parts.query,
            "".join(f"{name}:{headers[name].strip()}\n" for name in names),
            ";".join(names),
            payload_hash,
        ]
    )
    scope = f"{amz_date[:8]}/{region}/s3/aws4_request"
    to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical.encode()).hexdigest(),
        ]
    )
    key = ("AWS4" + secret).encode()
    for part in (amz_date[:8], region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return (
        scope,
        ";".join(names),
        hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest(),
    )


def put(body, env, now):
    url = f"{env['AWS_ENDPOINT_URL'].rstrip('/')}/{env['BUCKET']}/{KEY}"
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {
        "content-type": "application/json",
        "host": urlsplit(url).netloc,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    scope, signed, sig = signature(
        "PUT",
        url,
        headers,
        payload_hash,
        env["AWS_SECRET_ACCESS_KEY"],
        env["AWS_REGION"],
        amz_date,
    )
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={env['AWS_ACCESS_KEY_ID']}/{scope}, "
        f"SignedHeaders={signed}, Signature={sig}"
    )
    with urlopen(Request(url, data=body, headers=headers, method="PUT"), timeout=10):
        pass


def main():
    now = clock()
    env = os.environ
    results = {}
    for name, check in CHECKS.items():
        try:
            results[name] = bool(check(now, env))
        except Exception:  # Any read failure or missing input is down.
            results[name] = False
    if env.get("ALERT_TEST") == "true":
        results[TEST_COMPONENT] = False
    document = compose(results, now)
    put(json.dumps(document, separators=(",", ":")).encode(), env, now)
    print(json.dumps(document))


if __name__ == "__main__":
    main()
