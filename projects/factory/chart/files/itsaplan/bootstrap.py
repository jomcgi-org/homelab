"""Bootstrap itsaplan through its api (the factory chart's itsaplan-bootstrap).

Idempotent; every step reads before it writes:

1. Sign in as the break-glass admin from ADMIN_SECRET. On a database with no
   accounts, sign up instead: the first account is the instance admin.
2. Registration invite-only, and the OIDC provider from CONFIG with the
   client secret from OIDC_SECRET (written there by Authentik's secret-init),
   written when it differs from what STATE_SECRET records, or on the first
   sync more than a day after the last write.
3. The team and project from CONFIG, and the deferred label in bound projects.
4. Each external agent from CONFIG, working in that project. The api returns
   an agent's API key once, at creation or rotation, and this writes it to
   Secret AGENT_SECRET_PREFIX<username>. An agent whose Secret holds no
   working key gets a new key; an agent is never created twice.
   Verify the coordinator's identity and effective project permissions with
   its own key, granting missing rights through a dedicated team role.
5. Joe's sessions account, invited as a regular member of that project. Its
   password is saved in SESSIONS_SECRET before sign-up and never rotated.

Uses only the standard library and the pod's service account token.
"""

import base64
import hashlib
import json
import os
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

SA = os.environ.get("SA_DIR", "/var/run/secrets/kubernetes.io/serviceaccount")
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
# After this long, the next sync writes the OIDC settings again.
REWRITE_AFTER = 24 * 3600
# itsaplan 1.10.0's human username validator allows letters, digits, _ and .,
# but not hyphens. Keep the account's email and Secret named joe-sessions.
SESSIONS = {
    "email": "joe-sessions@jomcgi.dev",
    "name": "Joe (sessions)",
    "username": "joesessions",
}


class HTTPError(Exception):
    def __init__(self, method, url, status, body):
        # Responses can echo credentials, and invite URLs contain bearer tokens.
        super().__init__(f"{method} request failed: HTTP {status}")
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
    """A better-auth session, kept as the cookies the api sets."""

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
    # (STATE_SECRET) records a digest of what was last written and when. The
    # settings are written when a visible field or the digest differs, and on
    # the first sync more than REWRITE_AFTER after the last write, which also
    # undoes a client secret changed by hand in god mode.
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
        # Possibly stored settings encrypted with a key the api no longer
        # holds: every write decrypts them first and fails the same way.
        raise SystemExit(
            f"itsaplan cannot read its OIDC settings ({e.status}): possibly "
            "encrypted with another APP_ENCRYPTION_KEY. Check the api logs for "
            "a decrypt error before following the regeneration runbook "
            '(factory-cluster README, "itsaplan").'
        )
    matches = current.get("hasClientSecret") and all(
        current.get(k) == v for k, v in want.items()
    )
    state = secret_data(k8s("GET", state_name))
    written = float(state.get("oidcWrittenAt") or 0)
    fresh = time.time() - written < REWRITE_AFTER
    if matches and state.get("oidc") == digest and fresh:
        print("OIDC provider current")
    else:
        session.call(
            "PUT", "/god/oidc-settings", {**want, "clientSecret": client_secret}
        )
        write_secret(
            state_name, {"oidc": digest, "oidcWrittenAt": str(int(time.time()))}
        )
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


def ensure_deferred_labels(session, team_id):
    """Install the opt-in in every bound project, preserving existing labels."""
    for key in sorted(set(CONFIG.get("deferredProjects", []))):
        scoped = key if "." in key else f"{team_id}.{key}"
        path = "/projects/" + urllib.parse.quote(scoped, safe="")
        project = session.call("GET", path)
        if not any(label["name"] == "deferred" for label in project["labels"]):
            session.call("POST", path + "/labels", {"name": "deferred"})
            print(f"created deferred label in {key}")


def find_account(session, path, email):
    """Find an exact email in a paged account or membership listing."""
    page = 1
    while True:
        query = urllib.parse.urlencode(
            {"search": email, "kind": "all", "page": page, "pageSize": 100}
        )
        result = session.call("GET", f"{path}?{query}")
        for account in result["items"]:
            if account["email"].lower() == email.lower():
                return account
        if page * 100 >= result["total"]:
            return None
        page += 1


def sessions_invite(admin, project, email):
    """Reuse a pending invite, including after an interrupted sign-up."""
    base = f"{project}/invites"
    for invite in admin.call("GET", base):
        if invite["email"].lower() != email or invite["status"] != "pending":
            continue
        if (
            invite["role"] != "member"
            or invite["teamRole"] != "member"
            or invite["roleId"] is not None
        ):
            raise SystemExit("Joe's sessions invite must grant the default member role")
        # The invite view has an expiry flag computed by the server's clock.
        view = admin.call("GET", f"/invites/{invite['token']}")
        if view["expired"]:
            invite = admin.call("POST", f"{base}/{invite['id']}/renew", {})
        return invite
    return admin.call("POST", base, {"email": email, "role": "member"})


def ensure_sessions(admin, team_id):
    email = SESSIONS["email"]
    account = find_account(admin, "/god/users", email)
    name = os.environ["SESSIONS_SECRET"]
    credentials = secret_data(k8s("GET", name))
    if credentials.get("email", email) != email:
        raise SystemExit(f"{NAMESPACE}/{name} names a different account")
    if account and not all(credentials.get(k) for k in ("email", "password")):
        raise SystemExit(
            f"{email} already exists; restore {NAMESPACE}/{name}, "
            "refusing to replace its password"
        )
    if not account:
        credentials = {
            "email": email,
            "password": credentials.get("password") or secrets.token_urlsafe(32),
        }
        # Persist first: a failed sign-up or invite acceptance reuses this password.
        write_secret(name, credentials)

    project = f"/projects/{team_id}.{CONFIG['project']['key']}"
    member = find_account(admin, f"{project}/members", email)
    team_member = find_account(admin, f"/teams/{team_id}/members", email)
    invite = None
    if not member and not team_member:
        invite = sessions_invite(admin, project, email)

    session = Session()
    try:
        if account:
            # An invalid stored password fails here; never reset an existing account.
            session.call("POST", "/api/auth/sign-in/email", credentials)
        else:
            session.call("POST", "/api/auth/sign-up/email", {**credentials, **SESSIONS})
        me = session.call("GET", "/me")
        user = me.get("user") or {}
        if (
            not me.get("authenticated")
            or user.get("email") != email
            or user.get("role") != "user"
            or user.get("isAgent") is not False
            or user.get("username") != SESSIONS["username"]
        ):
            raise SystemExit("Joe's sessions account is not the expected regular user")
        if not member:
            if invite:
                session.call("POST", f"/invites/{invite['token']}/accept", {})
            else:
                admin.call(
                    "POST",
                    f"{project}/members",
                    {"userId": user["id"], "role": "member"},
                )
        access = session.call("GET", project)
        viewer = access["viewer"]
        if viewer != {"role": "member", "teamRole": "member", "via": "member"}:
            raise SystemExit("Joe's sessions account must be a regular project member")
        team = session.call("GET", f"/teams/{team_id}")
        workspaces = session.call("GET", "/workspaces")
        workspace = next(w for w in workspaces if w["id"] == team["workspaceId"])
        if workspace["role"] is not None:
            raise SystemExit("Joe's sessions account must not administer the workspace")
        permissions = access["permissions"].get("work_items", {})
        if not all(permissions.get(action) for action in ("read", "create", "edit")):
            raise SystemExit("Joe's sessions account cannot edit issues and comment")
        if user.get("name") != SESSIONS["name"]:
            session.call("POST", "/api/auth/update-user", {"name": SESSIONS["name"]})
        print(f"{SESSIONS['name']} (@{user['username']}) is a project member")
    finally:
        if session.cookies:
            session.call("POST", "/api/auth/sign-out", {})


def key_works(key, user_id=None):
    """True when the api resolves the key to an account (/me answers 200 either
    way, with authenticated false for a key it refuses)."""
    _, _, me = request("GET", API + "/me", headers={"x-api-key": key})
    return bool(
        me
        and me.get("authenticated")
        and (user_id is None or (me.get("user") or {}).get("id") == user_id)
    )


def ensure_scoped_role(session, team_id, role_name, actions):
    """A dedicated role, never the default role that coding agents inherit.

    Normalize against the live catalog, including extension resources.
    """
    catalog = session.call("GET", "/permission-catalog")
    permissions = {
        resource["key"]: {
            action: resource["key"] == "work_items" and action in actions
            for action in catalog["actions"]
        }
        for resource in catalog["resources"]
    }
    base = f"/teams/{team_id}/roles"
    roles = session.call("GET", base + "/options")
    matching = [r for r in roles if r["name"] == role_name]
    if len(matching) > 1 or any(r["system"] for r in matching):
        raise SystemExit(f"{role_name} must be one team-owned role")
    if not matching:
        return session.call(
            "POST", base, {"name": role_name, "permissions": permissions}
        )["id"]
    role = matching[0]
    if role["permissions"] != permissions:
        session.call("PATCH", f"{base}/{role['id']}", {"permissions": permissions})
    return role["id"]


def ensure_scoped_access(session, team_id, project_id, agent, role_id, project_key):
    """Remove other project grants, then assign the scoped role before publishing a key."""
    projects = [p["id"] for p in agent["projects"]]
    if any(p != project_id for p in projects):
        session.call(
            "PUT",
            f"/teams/{team_id}/ai-agents/{agent['id']}/projects",
            {"projectIds": [project_id] if project_id in projects else []},
        )
    project = urllib.parse.quote(project_key, safe="")
    base = f"/projects/{project}/members"
    want = {"role": "member", "roleId": role_id}
    if project_id not in projects:
        # New restricted agents start with no projects: they never acquire the
        # default role's writes, even briefly, while bootstrap runs.
        session.call("POST", base, {"userId": agent["userId"], **want})
    else:
        page = session.call(
            "GET", base + f"?kind=agent&search={agent['username']}&pageSize=100"
        )
        member = next(
            (m for m in page["items"] if m["userId"] == agent["userId"]), None
        )
        if member is None:
            raise SystemExit("scoped membership could not be verified")
        if any(member[k] != v for k, v in want.items()):
            user = urllib.parse.quote(agent["userId"], safe="")
            session.call("PATCH", f"{base}/{user}", want)


COORDINATOR_PERMISSIONS = {
    "work_items": {"read": True, "create": True, "edit": True},
    "ai_agents": {"read": True},
}


def has_coordinator_permissions(permissions):
    return all(
        permissions.get(resource, {}).get(action) is True
        for resource, actions in COORDINATOR_PERMISSIONS.items()
        for action in actions
    )


def is_coordinator_role(role):
    """Allow false entries from API normalization, but no unrelated grant."""
    permissions = role["permissions"]
    return (
        not role["system"]
        and has_coordinator_permissions(permissions)
        and all(
            enabled is False
            or COORDINATOR_PERMISSIONS.get(resource, {}).get(action) is True
            for resource, actions in permissions.items()
            for action, enabled in actions.items()
        )
    )


def coordinator_role(session, team_id, username):
    """Never patch a role: its users may span projects, invites and groups.

    Only reuse an unused role with exactly the required grants. Otherwise
    choose an unused name and create a separate role. A lost create response
    leaves an unused role that a retry can safely adopt.
    """
    base = f"/teams/{team_id}/roles"
    roles = session.call("GET", base + "/options")
    stem = f"Factory coordinator ({username})"
    unused = {"members": 0, "agents": 0, "invites": 0, "groups": 0}
    for suffix in range(len(roles) + 1):
        name = stem if suffix == 0 else f"{stem} {suffix}"
        matches = [r for r in roles if r["name"] == name]
        if matches:
            if len(matches) != 1 or not is_coordinator_role(matches[0]):
                continue
            role = matches[0]
        else:
            role = session.call(
                "POST", base, {"name": name, "permissions": COORDINATOR_PERMISSIONS}
            )
        path = f"{base}/{role['id']}"
        if session.call("GET", path + "/usage") != unused:
            continue
        # Recheck the role itself, not just its name or the earlier list.
        current = [
            r for r in session.call("GET", base + "/options") if r["id"] == role["id"]
        ]
        if len(current) != 1 or not is_coordinator_role(current[0]):
            raise SystemExit("coordinator role permissions changed; not assigned")
        return current[0]
    raise SystemExit("no unused coordinator role; retry with current role usage")


def ensure_coordinator_permissions(session, team_id, agent, key):
    """Use admin only to grant rights; verify effective access as the agent.

    Existing sufficient grants stay as-is. Missing rights get an unused role
    with only the required grants on this one project membership.
    """
    headers = {"x-api-key": key}
    _, _, me = request("GET", API + "/me", headers=headers)
    if (
        not me.get("authenticated")
        or (me.get("user") or {}).get("id") != agent["userId"]
    ):
        raise SystemExit("coordinator key does not identify the configured agent")
    project = f"/projects/{team_id}.{CONFIG['project']['key']}"
    _, _, access = request("GET", API + project, headers=headers)
    if not has_coordinator_permissions(access["permissions"]):
        role = coordinator_role(session, team_id, agent["username"])
        session.call(
            "PATCH",
            project + "/members/" + urllib.parse.quote(agent["userId"], safe=""),
            {"role": "member", "roleId": role["id"]},
        )
        _, _, access = request("GET", API + project, headers=headers)
    if not has_coordinator_permissions(access["permissions"]):
        raise SystemExit(
            "coordinator still lacks required effective project permissions"
        )
    print(
        f"verified {agent['username']} on {CONFIG['project']['key']}: "
        "work_items read/create/edit, ai_agents read"
    )


def ensure_agents(session, team_id, project_id):
    coordinator = CONFIG.get("coordinator", "")
    if coordinator and coordinator not in [a["username"] for a in CONFIG["agents"]]:
        raise SystemExit("coordinator is not a configured bootstrap agent")
    base = f"/teams/{team_id}/ai-agents"
    existing = {a["username"].lower(): a for a in session.call("GET", base)}
    for want in CONFIG["agents"]:
        username = want["username"]
        reviewer = username == "factory-reviewer"
        sync = username == "intsec-sync"
        restricted = reviewer or sync
        target_key = "INTSEC" if sync else CONFIG["project"]["key"]
        target_id = project_id
        if sync:
            setup = session.call("GET", "/projects/INTSEC")
            target_id = setup["project"]["id"]
            if setup["project"]["teamId"] != team_id:
                raise SystemExit("INTSEC belongs to another team")
            if not any(label["name"] == "semgrep" for label in setup["labels"]):
                session.call("POST", "/projects/INTSEC/labels", {"name": "semgrep"})
            role_id = ensure_scoped_role(
                session, team_id, "INTSEC sync", ("read", "create")
            )
        elif reviewer:
            role_id = ensure_scoped_role(
                session, team_id, "Factory reviewer", ("read",)
            )
        name = os.environ["AGENT_SECRET_PREFIX"] + username
        settings = {
            "name": want["name"],
            "triggerOnAssign": want.get("triggerOnAssign", False),
            "triggerOnMention": want.get("triggerOnMention", False),
            # Runs from any member of the team, not only the admin's.
            "runnerScope": "team",
            "chatEnabled": False,
        }
        if restricted:
            settings.update(
                triggerOnAssign=False, triggerOnMention=False, fieldTriggers=[]
            )
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
                    "projectIds": [] if restricted else [project_id],
                },
            )
            agent, key = created["agent"], created["apiKey"]
            print(f"created agent {username} ({agent['id']})")
        else:
            if restricted and agent["kind"] != "external":
                raise SystemExit(f"{username} must be an external agent")
            projects = [p["id"] for p in agent["projects"]]
            drift = {k: v for k, v in settings.items() if agent.get(k, v) != v}
            if drift:
                agent = session.call("PATCH", f"{base}/{agent['id']}", drift)
                print(f"updated agent {username}: {', '.join(drift)}")
            if not restricted and project_id not in projects:
                agent = session.call(
                    "PUT",
                    f"{base}/{agent['id']}/projects",
                    {"projectIds": projects + [project_id]},
                )
                print(f"attached agent {username} to the project")
            stored = secret_data(k8s("GET", name)).get("apiKey")
            if stored and key_works(stored, agent["userId"] if restricted else None):
                key = stored
            else:
                rotated = session.call("POST", f"{base}/{agent['id']}/regenerate-key")
                key = rotated["apiKey"]
                print(f"rotated {username}'s key: its Secret held none that works")
        if restricted:
            ensure_scoped_access(
                session, team_id, target_id, agent, role_id, target_key
            )
        write_secret(
            name,
            {
                "apiKey": key,
                "agentId": str(agent["id"]),
                "teamId": str(team_id),
                "projectKey": target_key,
                "url": API,
            },
        )
        if username == coordinator:
            ensure_coordinator_permissions(session, team_id, agent, key)


def main():
    wait_for_api()
    admin = wait_for_secret(os.environ["ADMIN_SECRET"], ["email", "password"])
    oidc = wait_for_secret(os.environ["OIDC_SECRET"], ["client-secret"])
    session = sign_in(admin)
    try:
        ensure_settings(session, oidc["client-secret"])
        team_id = ensure_team(session)
        project_id = ensure_project(session, team_id)
        ensure_deferred_labels(session, team_id)
        ensure_agents(session, team_id, project_id)
        ensure_sessions(session, team_id)
    finally:
        # Ends the session, so reruns leave no session rows behind.
        session.call("POST", "/api/auth/sign-out", {})


if __name__ == "__main__":
    main()
