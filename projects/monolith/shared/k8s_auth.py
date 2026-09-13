"""In-cluster caller authentication for TokenReview-gated services (fc-invoke).

The fc-invoke daemon authenticates callers via the Kubernetes TokenReview API
(STPA: unauthenticated /invoke), so a request must carry this pod's
ServiceAccount bearer token. Kubernetes mounts short-lived, auto-rotated
projected tokens; we read the configured file fresh per call (a cheap file read)
so a rotated token is always current rather than a value cached at import.

Outside a cluster (local dev, unit tests) the token file is absent, so
``auth_headers`` returns an empty dict and the caller's behaviour is unchanged:
the request simply carries no Authorization header. In-cluster the header is
always present, which is what the daemon enforces.
"""

from __future__ import annotations

import os

# Standard projected-token mount path for a pod's ServiceAccount. Components
# with an audience-scoped projection override this with K8S_AUTH_TOKEN_FILE.
_DEFAULT_SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_TOKEN_FILE_ENV = "K8S_AUTH_TOKEN_FILE"


def service_account_token(path: str | None = None) -> str | None:
    """Return the pod ServiceAccount token, or None when unavailable.

    ``K8S_AUTH_TOKEN_FILE`` selects a component-specific projection when no
    explicit path is passed. Reads the file fresh so kubelet rotation is picked
    up. Any read error yields None rather than raising, so callers degrade to
    unauthenticated requests locally.
    """
    token_path = path or os.environ.get(_TOKEN_FILE_ENV, _DEFAULT_SA_TOKEN_PATH)
    try:
        with open(token_path, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        return None
    return token or None


def auth_headers(path: str | None = None) -> dict[str, str]:
    """Return the Authorization header for an in-cluster call, or {} off-cluster."""
    token = service_account_token(path)
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}
