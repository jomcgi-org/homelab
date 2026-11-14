"""Bootstrap itsaplan through its api (the factory chart's itsaplan-bootstrap).

Idempotent; every step reads before it writes:

1. Sign in as the break-glass admin from ADMIN_SECRET. On a database with no
   accounts, sign up instead: the first account is the instance admin.
2. Registration invite-only, and the OIDC provider from CONFIG with the
   client secret from OIDC_SECRET (written there by Authentik's secret-init),
   written only when it differs from what STATE_SECRET records.
3. The team and project from CONFIG, in the admin's workspace.
4. Each external agent from CONFIG, working in that project. The api returns
   an agent's API key once, at creation or rotation, and this writes it to
   Secret AGENT_SECRET_PREFIX<username>. An agent whose Secret holds no
   working key gets a new key; an agent is never created twice.

Uses only the standard library and the pod's service account token.
"""

import base64
import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
K8S = "https://{}:{}".format(
    os.environ["KUBERNETES_SERVICE_HOST"], os.environ["KUBERNETES_SERVICE_PORT"]
)
with open(f"{SA}/token") as f:
    K8S_TOKEN = f.read()
K8S_CTX = ssl.create_default_context(cafile=f"{SA}/ca.crt")
NAMESPACE = os.environ["NAMESPACE"]
API = os.environ["API_INTERNAL_URL"].rstrip("/")
# better-auth accepts state-changing calls only from a trusted origin.
ORIGIN = os.environ["APP_URL"].rstrip("/")
with open(os.environ["CONFIG"]) as f:
    CONFIG = json.load(f)


class HTTPError(Exception):
    def __init__(self, method, url, status, body):
        super().__init__(f"{method} {url}: {status} {body[:500]}")
        self.status = status
        self.body = body


def request(method, url, body=None, headers=None, ctx=None):
    req = urllib.request.Request(
        url,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            raw = resp.read()
            return resp.status, resp.headers, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise HTTPError(method, url, e.code, e.read().decode(errors="replace"))


# Kubernetes


def secret_path(name):
    return f"{K8S}/api/v1/namespaces/{NAMESPACE}/secrets/{name}"


def k8s(method, name, body=None):
    _, _, secret = request(
        method,
        secret_path(name),
        body,
        {"Authorization": f"Bearer {K8S_TOKEN}"},
        K8S_CTX,
    )
    return secret


def secret_data(secret):
    return {
        k: base64.b64decode(v).decode() for k, v in (secret.get("data") or {}).items()
    }


def wait_for_secret(name, keys, timeout=900):
    """Return a Secret's decoded data once it holds every key in keys."""
    deadline = time.monotonic() + timeout
    while True:
        data = secret_data(k8s("GET", name))
        if all(data.get(k) for k in keys):
            return data
        if time.monotonic() > deadline:
            raise SystemExit(f"{NAMESPACE}/{name} still lacks {', '.join(keys)}")
        print(f"waiting for {', '.join(keys)} in {NAMESPACE}/{name}")
        time.sleep(10)


def write_secret(name, want):
    """Set a Secret's data to want; a no-op when it already holds it."""
    secret = k8s("GET", name)
    encoded = {k: base64.b64encode(v.encode()).decode() for k, v in want.items()}
    if secret.get("data") == encoded:
        print(f"{NAMESPACE}/{name} current")
        return
    secret["data"] = encoded
    k8s("PUT", name, secret)
    print(f"updated {NAMESPACE}/{name}")


# itsaplan


class Session:
    """The admin's better-auth session, kept as the cookies the api sets."""

    def __init__(self):
        self.cookies = {}

    def call(self, method, path, body=None, headers=None):
        hdrs = {"Origin": ORIGIN, **(headers or {})}
        if self.cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        _, resp_headers, data = request(method, API + path, body, hdrs)
        for cookie in resp_headers.get_all("Set-Cookie") or []:
            name, _, rest = cookie.partition("=")
            self.cookies[name.strip()] = rest.split(";", 1)[0]
        return data


def wait_for_api(timeout=600):
    deadline = time.monotonic() + timeout
    while True:
        try:
            request("GET", API + "/")
            return
        except (HTTPError, OSError) as e:
            if time.monotonic() > deadline:
                raise SystemExit(f"the api never answered: {e}")
            print(f"waiting for the api: {e}")
            time.sleep(5)


def sign_in(admin):
    session = Session()
    credentials = {"email": admin["email"], "password": admin["password"]}
    try:
        session.call("POST", "/api/auth/sign-in/email", credentials)
        print(f"signed in as {admin['email']}")
    except HTTPError as e:
        if e.status != 401:
            raise
        # No such account: on a fresh database the first sign-up becomes the
        # instance admin. Anywhere else it is refused, and the run stops.
        if session.call("GET", "/auth-config").get("hasUsers"):
            raise SystemExit(
                f"{admin['email']} cannot sign in and the instance already has "
                "accounts; restore the admin Secret or reset its password"
            )
        session.call(
            "POST",
            "/api/auth/sign-up/email",
            {**credentials, "name": CONFIG["admin"]["name"]},
        )
        print(f"created {admin['email']}")
    me = session.call("GET", "/me")
    if (me.get("user") or {}).get("role") != "god":
        raise SystemExit(f"{admin['email']} is not the instance admin")
    return session


def ensure_settings(session, client_secret):
    current = session.call("GET", "/god/auth-settings")
    if current.get("registration") != "invite":
        session.call("PUT", "/god/auth-settings", {"registration": "invite"})
        print("registration is now invite-only")
    else:
        print("registration invite-only")

    # itsaplan cannot return the stored client secret, so the state Secret
    # (STATE_SECRET) records a digest of what was last written. The settings
    # are written only when the visible fields, the client secret or that
    # digest differ, or when itsaplan cannot read them back (a replaced
    # APP_ENCRYPTION_KEY leaves the stored secret undecryptable).
    oidc = CONFIG["oidc"]
    want = {
        "enabled": True,
        "label": oidc["label"],
        "discoveryUrl": oidc["discoveryUrl"],
        "clientId": oidc["clientId"],
        "scopes": ["openid", "email", "profile"],
        "pkce": True,
    }
    digest = hashlib.sha256(
        json.dumps({**want, "clientSecret": client_secret}, sort_keys=True).encode()
    ).hexdigest()
    state_name = os.environ["STATE_SECRET"]
    try:
        current = session.call("GET", "/god/oidc-settings")
    except HTTPError as e:
        if e.status < 500:
            raise
        print(f"itsaplan cannot read its OIDC settings ({e.status}); rewriting them")
        current = {}
    matches = current.get("hasClientSecret") and all(
        current.get(k) == v for k, v in want.items()
    )
    if matches and secret_data(k8s("GET", state_name)).get("oidc") == digest:
        print("OIDC provider current")
    else:
        session.call(
            "PUT", "/god/oidc-settings", {**want, "clientSecret": client_secret}
        )
        write_secret(state_name, {"oidc": digest})
        current = session.call("GET", "/god/oidc-settings")
        print("OIDC provider written")
    print(
        f"OIDC provider {current['label']!r} enabled={current['enabled']} "
        f"redirect URI {current['redirectUri']}"
    )


def ensure_team(session):
    want = CONFIG["team"]
    for team in session.call("GET", "/teams"):
        if team.get("slug") == want["slug"]:
            print(f"team {want['slug']} is {team['id']}")
            return team["id"]
    workspaces = [
        w for w in session.call("GET", "/workspaces") if w.get("role") == "owner"
    ]
    if not workspaces:
        raise SystemExit("the admin owns no workspace to hold the team")
    team = session.call(
        "POST",
        "/teams",
        {
            "name": want["name"],
            "slug": want["slug"],
            "workspaceId": workspaces[0]["id"],
        },
    )
    print(f"created team {want['slug']} ({team['id']})")
    return team["id"]


def ensure_project(session, team_id):
    want = CONFIG["project"]
    query = urllib.parse.urlencode({"search": want["key"]})
    page = session.call("GET", f"/teams/{team_id}/projects?{query}")
    for project in page.get("items", []):
        if project.get("key") == want["key"]:
            print(f"project {want['key']} is {project['id']}")
            return project["id"]
    body = {"key": want["key"], "name": want["name"]}
    if want.get("description"):
        body["description"] = want["description"]
    project = session.call("POST", f"/teams/{team_id}/projects", body)
    print(f"created project {want['key']} ({project['id']})")
    return project["id"]


def key_works(key):
    """True when the api resolves the key to an account (/me answers 200 either
    way, with authenticated false for a key it refuses)."""
    _, _, me = request("GET", API + "/me", headers={"x-api-key": key})
    return bool(me and me.get("authenticated"))


def ensure_agents(session, team_id, project_id):
    base = f"/teams/{team_id}/ai-agents"
    existing = {a["username"].lower(): a for a in session.call("GET", base)}
    for want in CONFIG["agents"]:
        username = want["username"]
        name = os.environ["AGENT_SECRET_PREFIX"] + username
        settings = {
            "name": want["name"],
            "triggerOnAssign": want.get("triggerOnAssign", False),
            "triggerOnMention": want.get("triggerOnMention", False),
            # Runs from any member of the team, not only the admin's.
            "runnerScope": "team",
            "chatEnabled": False,
        }
        agent = existing.get(username.lower())
        key = None
        if agent is None:
            created = session.call(
                "POST",
                base,
                {
                    **settings,
                    "username": username,
                    "kind": "external",
                    "projectIds": [project_id],
                },
            )
            agent, key = created["agent"], created["apiKey"]
            print(f"created agent {username} ({agent['id']})")
        else:
            projects = [p["id"] for p in agent["projects"]]
            drift = {k: v for k, v in settings.items() if agent.get(k, v) != v}
            if drift:
                agent = session.call("PATCH", f"{base}/{agent['id']}", drift)
                print(f"updated agent {username}: {', '.join(drift)}")
            if project_id not in projects:
                agent = session.call(
                    "PUT",
                    f"{base}/{agent['id']}/projects",
                    {"projectIds": projects + [project_id]},
                )
                print(f"attached agent {username} to the project")
            stored = secret_data(k8s("GET", name)).get("apiKey")
            if stored and key_works(stored):
                key = stored
            else:
                rotated = session.call("POST", f"{base}/{agent['id']}/regenerate-key")
                key = rotated["apiKey"]
                print(f"rotated {username}'s key: its Secret held none that works")
        write_secret(
            name,
            {
                "apiKey": key,
                "agentId": str(agent["id"]),
                "teamId": str(team_id),
                "projectKey": CONFIG["project"]["key"],
                "url": API,
            },
        )


def main():
    wait_for_api()
    admin = wait_for_secret(os.environ["ADMIN_SECRET"], ["email", "password"])
    oidc = wait_for_secret(os.environ["OIDC_SECRET"], ["client-secret"])
    session = sign_in(admin)
    try:
        ensure_settings(session, oidc["client-secret"])
        team_id = ensure_team(session)
        project_id = ensure_project(session, team_id)
        ensure_agents(session, team_id, project_id)
    finally:
        # Ends the session, so reruns leave no session rows behind.
        session.call("POST", "/api/auth/sign-out", {})


if __name__ == "__main__":
    main()
