"""In-cluster caller authentication for TokenReview-gated services (fc-invoke).

The fc-invoke daemon authenticates callers via the Kubernetes TokenReview API
(STPA: unauthenticated /invoke), so a request must carry this pod's
ServiceAccount bearer token. Kubernetes mounts a short-lived, auto-rotated
projected token at the standard path; we read it fresh per call (a cheap file
read) so a rotated token is always current rather than a value cached at import.

Outside a cluster (local dev, unit tests) the token file is absent, so
``auth_headers`` returns an empty dict and the caller's behaviour is unchanged:
the request simply carries no Authorization header. In-cluster the header is
always present, which is what the daemon enforces.
"""

from __future__ import annotations

# Standard projected-token mount path for a pod's ServiceAccount.
_SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"


def service_account_token(path: str = _SA_TOKEN_PATH) -> str | None:
    """Return the pod ServiceAccount token, or None when unavailable.

    Reads the projected token file fresh so kubelet rotation is picked up. Any
    read error (file absent off-cluster, permissions) yields None rather than
    raising, so callers degrade to unauthenticated requests locally.
    """
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        return None
    return token or None


def auth_headers(path: str = _SA_TOKEN_PATH) -> dict[str, str]:
    """Return the Authorization header for an in-cluster call, or {} off-cluster."""
    token = service_account_token(path)
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}
