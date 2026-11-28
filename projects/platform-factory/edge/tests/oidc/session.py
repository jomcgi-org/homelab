"""Exercise the browser redirects and carry OAuth2's encrypted cookies across them."""

import datetime
import hashlib
import hmac
from http.client import HTTPConnection
import os
from http.cookies import SimpleCookie
import json
import subprocess
import time
from urllib.parse import parse_qs, quote, urlencode, urlsplit


def session(host, subject, expected=200, path="/"):
    cookies = SimpleCookie()

    def get(path):
        conn = HTTPConnection("localhost", 18080, timeout=10)
        conn.request(
            "GET",
            path,
            headers={
                "Host": host,
                "Cookie": "; ".join(
                    item.OutputString(attrs=[]) for item in cookies.values()
                ),
            },
        )
        response = conn.getresponse()
        for name, value in response.getheaders():
            if name.lower() == "set-cookie":
                cookies.load(value)
        body = response.read()
        conn.close()
        return response.status, response.getheader("Location"), body

    # Routes reach the data plane asynchronously. Retry only the unsigned
    # first request; callback, authorization and upstream assertions run once.
    deadline = time.monotonic() + 60
    while True:
        try:
            status, redirect, _ = get(path)
        except (ConnectionError, TimeoutError):
            if time.monotonic() >= deadline:
                raise
        else:
            if status == 302:
                break
            if time.monotonic() >= deadline:
                raise AssertionError((host, "expected sign-in redirect", status))
        time.sleep(1)
    auth = urlsplit(redirect)
    assert auth.hostname == "issuer.edge.svc.cluster.local"
    query = parse_qs(auth.query)
    callback = urlsplit(query["redirect_uri"][0])
    assert callback.hostname == host
    status, _, _ = get(
        callback.path + "?" + urlencode({"code": subject, "state": query["state"][0]})
    )
    assert status == 302 and cookies["factory-edge-id-token"].value
    status, _, body = get(path)
    if subject == "applicant" or expected == 403:
        assert status == 403, (host, subject, status)
    elif host.startswith("factory-agents."):
        assert (
            status == 200
            and json.loads(body)["authorization"] == "Bearer fixture-access-operator"
        )
    else:
        assert status == 200 and json.loads(body) == {"authorization": None}, (
            host,
            status,
        )
    print(host, path, subject, status)
    return get


for hostname in [
    "factory.jomcgi.dev",
    "factory-argocd.jomcgi.dev",
    "factory-plan.jomcgi.dev",
    "factory-runbooks.jomcgi.dev",
    "factory-grafana.jomcgi.dev",
    "factory-agents.jomcgi.dev",
]:
    for role in ["operator", "applicant"]:
        session(hostname, role)

# The shared gateway accepts factory operators, while T3 requires admins.
session("factory.jomcgi.dev", "factory-operator")
admin_request = session("t3.jomcgi.dev", "operator")
for role in ["factory-operator", "applicant"]:
    session("t3.jomcgi.dev", role, expected=403)

# artifact.jomcgi.dev: a Discord server's members read its folder only.
# Each folder's policy merges into the gateway sign-in, so one session made
# on any path is valid on every path; authorization decides per folder.
homelab, other = "1501965852042330302", "185442367703220224"
member = session("artifact.jomcgi.dev", "member", path=f"/{homelab}/page.html")
for path in [
    f"/{other}/page.html",
    "/private/page.html",
    "/",
    f"/{homelab}/../private/page.html",
    f"/{homelab}page.html",
]:
    status, _, _ = member(path)
    assert status == 403, (path, status)
    print("artifact.jomcgi.dev", path, "member", status)
admin = session("artifact.jomcgi.dev", "operator", path="/private/page.html")
for path in [f"/{other}/page.html", f"/{homelab}/page.html", "/"]:
    status, _, body = admin(path)
    assert status == 200 and json.loads(body) == {"authorization": None}, (path, status)
    print("artifact.jomcgi.dev", path, "operator", status)
for role in ["factory-operator", "applicant"]:
    session("artifact.jomcgi.dev", role, expected=403, path=f"/{homelab}/page.html")
# notes.jomcgi.dev: the same per-server folders (HOME-920).
notes = session("notes.jomcgi.dev", "member", path=f"/{homelab}/")
status, _, _ = notes(f"/{homelab}")
assert status == 200, ("bare server path", status)
for path in [f"/{other}/", "/", f"/{homelab}/../{other}/", f"/{homelab}x/"]:
    status, _, _ = notes(path)
    assert status == 403, (path, status)
    print("notes.jomcgi.dev", path, "member", status)
for role in ["factory-operator", "applicant"]:
    session("notes.jomcgi.dev", role, expected=403, path=f"/{homelab}/")
session("notes.jomcgi.dev", "operator", path=f"/{other}/")
# The member's groups admit them on no other host.
session("factory.jomcgi.dev", "member", expected=403)


# Signed share links (route artifact-share). The link is /s/<key>; it is
# signed for what Garage sees after the edge: Host artifact.jomcgi.dev and
# path /bosun-artifacts/<key> (projects/factory/discord/artifacts/share.go).
def presign(key, secret_id, secret, ttl=3600, at=None):
    at = at or datetime.datetime.now(datetime.timezone.utc)
    amz = at.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{amz[:8]}/garage/s3/aws4_request"
    params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{secret_id}/{scope}",
        "X-Amz-Date": amz,
        "X-Amz-Expires": str(ttl),
        "X-Amz-SignedHeaders": "host",
    }
    query = "&".join(
        f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in sorted(params.items())
    )
    canonical = "\n".join(
        [
            "GET",
            "/bosun-artifacts/" + key,
            query,
            "host:artifact.jomcgi.dev\n",
            "host",
            "UNSIGNED-PAYLOAD",
        ]
    )
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    k = ("AWS4" + secret).encode()
    for part in (amz[:8], "garage", "s3", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"/s/{key}?{query}&X-Amz-Signature={signature}"


def anonymous(path, method="GET"):
    conn = HTTPConnection("localhost", 18080, timeout=10)
    conn.request(method, path, headers={"Host": "artifact.jomcgi.dev"})
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, dict(response.getheaders()), body


report = "1501965852042330302/1501965852042330999/report.html"
ro_id, ro_secret = os.environ["SHARE_RO_ID"], os.environ["SHARE_RO_SECRET"]
link = presign(report, ro_id, ro_secret)
deadline = time.monotonic() + 60
while True:
    status, headers, body = anonymous(link)
    if status == 200 or time.monotonic() >= deadline:
        break
    time.sleep(1)
assert status == 200 and body == b"<h1>shared</h1>", (status, body[:200])
lower = {k.lower(): v for k, v in headers.items()}
assert lower["content-security-policy"].startswith(
    "sandbox allow-scripts allow-popups;"
), lower
assert lower["x-content-type-options"] == "nosniff"
assert lower["referrer-policy"] == "no-referrer"
assert lower["cross-origin-resource-policy"] == "same-origin"
assert lower["x-robots-tag"] == "noindex, nofollow"
assert lower["cache-control"] == "private, no-store"
assert "set-cookie" not in lower
print("artifact.jomcgi.dev share link 200")
# The link is signed for GET only: SigV4 signs the method, so a HEAD of the
# same link does not match and must not be served.
head = anonymous(link, "HEAD")[0]
assert 400 <= head < 500, head
tampered = link[:-1] + ("0" if link[-1] != "0" else "1")
past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
for name, path in {
    "tampered": tampered,
    "other path, same signature": link.replace("report.html", "other.html", 1),
    "unsigned": "/s/" + report,
    "unsigned list": "/s/",
    "wrong key": presign(report, ro_id, "0" * 64),
    "known key ID, wrong secret": presign(report, os.environ["SHARE_RW_ID"], "x"),
}.items():
    status, _, _ = anonymous(path)
    assert status == 403, (name, status)
    print("artifact.jomcgi.dev share", name, status)
# Garage v2.5.0 refuses an expired link while it parses the query, as a bad
# request ("Date is too old"), before it checks the signature.
status, _, body = anonymous(presign(report, ro_id, ro_secret, ttl=60, at=past))
assert status == 400 and b"<h1>shared</h1>" not in body, ("expired", status)
print("artifact.jomcgi.dev share expired", status)
# Only GET and HEAD skip sign-in; anything else meets the Authentik policy.
status, _, _ = anonymous(link, "DELETE")
assert status == 302, ("DELETE", status)
# The normal paths still need sign-in.
for path in ["/" + report, "/private/x.html", "/"]:
    status, headers, _ = anonymous(path)
    assert status == 302, (path, status)
print("artifact.jomcgi.dev without a link: sign-in redirect")

# The signed admin cookie used above must fail closed if the route policy is gone.
subprocess.run(
    [
        "kubectl",
        "--context",
        "kind-edge-oidc",
        "-n",
        "edge",
        "delete",
        "securitypolicy",
        "t3-authentik-oidc",
    ],
    check=True,
)
deadline = time.monotonic() + 60
while True:
    status, _, _ = admin_request("/")
    if status == 403:
        print("t3.jomcgi.dev admin without route policy 403")
        break
    if status == 302:
        # The Gateway OAuth filter can invalidate the route filter's cookie.
        # Finish a fresh admin login to distinguish a redirect from denial.
        session("t3.jomcgi.dev", "operator", expected=403)
        print("t3.jomcgi.dev admin without route policy 403 after reauthentication")
        break
    if time.monotonic() >= deadline:
        raise AssertionError(("T3 without route policy should deny the admin", status))
    time.sleep(1)
