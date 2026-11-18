"""Publish factory health as static data (HOME-260).

Each run reads the factory cluster's own state and writes health.json to the
Garage bucket factory-public. The mirror copies it to R2 with a 60-second
cache, so public readers never call the cluster.
"""

import datetime
import hashlib
import hmac
import json
import os
import ssl
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

SERVICE_ACCOUNT = "/var/run/secrets/kubernetes.io/serviceaccount"
APPLICATIONS = "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/"
KEY = "health.json"


def kube(path):
    with open(SERVICE_ACCOUNT + "/token") as token:
        request = Request(
            "https://kubernetes.default.svc" + path,
            headers={"Authorization": "Bearer " + token.read().strip()},
        )
    context = ssl.create_default_context(cafile=SERVICE_ACCOUNT + "/ca.crt")
    with urlopen(request, context=context, timeout=10) as response:
        return json.load(response)


def recent(timestamp, seconds, now):
    observed = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return 0 <= (now - observed).total_seconds() <= seconds


def cluster(now):
    nodes = kube("/api/v1/nodes")["items"]
    ready = bool(nodes) and all(
        any(
            c["type"] == "Ready" and c["status"] == "True"
            for c in node["status"]["conditions"]
        )
        for node in nodes
    )
    status = kube(APPLICATIONS + "argocd")["status"]
    return (
        ready
        and status["health"]["status"] == "Healthy"
        and recent(status["reconciledAt"], 900, now)
    )


def site(now):
    status = kube(APPLICATIONS + "jomcgi-site-prod")["status"]
    operation = status["operationState"]
    # The last sync must have deployed the revision Argo CD now wants.
    return (
        status["health"]["status"] == "Healthy"
        and status["sync"]["status"] == "Synced"
        and operation["phase"] == "Succeeded"
        and operation["syncResult"]["revision"] == status["sync"]["revision"]
    )


CHECKS = {"factory-cluster": cluster, "jomcgi-site": site}


def compose(results, now):
    """Return the public document. It names components and states only."""
    components = {
        name: "healthy" if ok else "unhealthy" for name, ok in sorted(results.items())
    }
    healthy = all(value == "healthy" for value in components.values())
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
    now = datetime.datetime.now(datetime.timezone.utc)
    results = {}
    for name, check in CHECKS.items():
        try:
            results[name] = bool(check(now))
        except Exception:  # Any read failure is an unhealthy component.
            results[name] = False
    document = compose(results, now)
    put(json.dumps(document, separators=(",", ":")).encode(), os.environ, now)
    print(json.dumps(document))


if __name__ == "__main__":
    main()
