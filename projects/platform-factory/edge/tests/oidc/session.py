"""Exercise the browser redirects and carry OAuth2's encrypted cookies across them."""

from http.client import HTTPConnection
from http.cookies import SimpleCookie
import json
import time
from urllib.parse import parse_qs, urlencode, urlsplit


def session(host, subject):
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
    if subject == "applicant":
        assert status == 403, (host, status)
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


for hostname in [
    "factory.jomcgi.dev",
    "factory-argocd.jomcgi.dev",
    "factory-plan.jomcgi.dev",
    "factory-grafana.jomcgi.dev",
    "factory-agents.jomcgi.dev",
]:
    for role in ["operator", "applicant"]:
        session(hostname, role)
