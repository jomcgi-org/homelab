"""Exercise the browser redirects and carry OAuth2's encrypted cookies across them."""

from http.client import HTTPConnection
from http.cookies import SimpleCookie
import json
import subprocess
import time
from urllib.parse import parse_qs, urlencode, urlsplit


def session(host, subject, expected=200):
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

    path = "/"
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
    print(host, subject, status)
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
    if time.monotonic() >= deadline:
        raise AssertionError(("T3 without route policy should deny the admin", status))
    time.sleep(1)
