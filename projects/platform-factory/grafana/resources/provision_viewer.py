"""Publish a fresh Viewer credential when the Grafana pod starts."""

import base64
import json
import secrets
import ssl
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    base = "http://127.0.0.1:3000"
    headers = {"Content-Type": "application/json"}

    def api(path, body=None, method=None):
        request = Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        return urlopen(request, timeout=10)

    password = Path("/run/grafana/admin-password").read_text()
    with api("/login", {"user": "admin", "password": password}) as response:
        cookies = SimpleCookie()
        for cookie in response.headers.get_all("Set-Cookie", []):
            cookies.load(cookie)
        headers["Cookie"] = "grafana_session=" + cookies["grafana_session"].value

    name = "factory-mcp"
    with api("/api/serviceaccounts/search?query=factory-mcp") as response:
        account = next(
            (a for a in json.load(response)["serviceAccounts"] if a["name"] == name),
            None,
        )
    if account is None:
        with api("/api/serviceaccounts", {"name": name, "role": "Viewer"}) as response:
            account = json.load(response)
    if account["role"] != "Viewer":
        raise ValueError("Expected Viewer service account")
    tokens = f"/api/serviceaccounts/{account['id']}/tokens"
    with api(tokens) as response:
        for token in json.load(response):
            if token["name"] == name:
                api(f"{tokens}/{token['id']}", method="DELETE").close()
    with api(tokens, {"name": name}) as response:
        viewer = json.load(response)["key"].encode()
    sa = Path("/run/kube")
    context = ssl.create_default_context(cafile=str(sa / "ca.crt"))

    def secret_request(name, patch=None):
        request = Request(
            f"https://kubernetes.default.svc/api/v1/namespaces/grafana/secrets/{name}",
            method="PATCH" if patch else "GET",
            data=json.dumps(patch).encode() if patch else None,
            headers={
                "Authorization": "Bearer " + (sa / "token").read_text(),
                "Content-Type": "application/merge-patch+json",
            },
        )
        with urlopen(request, context=context, timeout=10) as response:
            return json.load(response)

    secret_request(
        "mcp-grafana-viewer", {"data": {"token": base64.b64encode(viewer).decode()}}
    )
    # Keep the gateway credential across Grafana's ephemeral database resets.
    caller = secret_request("mcp-grafana-caller")
    if not caller.get("data", {}).get("caller"):
        secret_request(
            "mcp-grafana-caller",
            {
                "metadata": {"resourceVersion": caller["metadata"]["resourceVersion"]},
                "stringData": {"caller": secrets.token_urlsafe(32)},
            },
        )


if __name__ == "__main__":
    main()
    while True:
        time.sleep(86400)
