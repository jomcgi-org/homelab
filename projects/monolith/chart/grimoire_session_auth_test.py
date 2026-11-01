"""Grimoire sign-in renews in place and a fetch never follows a login redirect."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent


def grimoire_policy(deploy):
    command = [os.environ.get("HELM_BIN", "helm"), "template", "kg", str(CHART)]
    if deploy:
        values = os.environ.get(
            "DEPLOY_VALUES", str(CHART.parent / "deploy/values.yaml")
        )
        command += ["-f", values]
    else:
        command += ["--set", "cfIngress.grimoire.enabled=true"]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr
    return next(
        doc
        for doc in yaml.safe_load_all(result.stdout)
        if doc
        and doc["kind"] == "SecurityPolicy"
        and doc["metadata"]["name"].endswith("-grimoire")
    )


@pytest.mark.parametrize("deploy", (False, True))
def test_oidc_refreshes_tokens_and_answers_fetches_with_401(deploy):
    spec = grimoire_policy(deploy)["spec"]
    oidc = spec["oidc"]
    # authentik issues a refresh token only for offline_access.
    assert oidc["scopes"] == ["email", "profile", "offline_access"]
    assert oidc["refreshToken"] is True
    # Only fetch/XHR (Sec-Fetch-Mode: cors) skips the login redirect; a page
    # navigation sends "navigate" and still reaches authentik.
    assert oidc["denyRedirect"] == {
        "headers": [{"name": "sec-fetch-mode", "type": "Exact", "value": "cors"}]
    }
    # Envoy writes renewed tokens into the request cookies, so the JWT check
    # keeps reading the same cookie.
    assert oidc["cookieNames"]["idToken"] == "grimoire-id-token"
    assert spec["jwt"]["providers"][0]["extractFrom"] == {
        "cookies": ["grimoire-id-token"]
    }
