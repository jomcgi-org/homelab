"""Guard (ADR 005): the Turnstile secret stays in the backend, not the frontend.

ADR 005 layer 1 / Security: siteverify runs in the FastAPI "web" binary and the
Turnstile *secret* (and the ip-hash salt) live there only. The site *key* is
public by design and is the only Turnstile value the SSR "frontend" component may
carry, as a plain literal (never a secretKeyRef to the Turnstile secret object).

This reads the monolith-public chart's values.yaml and fails CI if a future edit
either drops the backend secretKeyRefs or leaks the Turnstile secret into the
frontend. Reading values.yaml (not a rendered manifest) keeps the test free of a
Helm toolchain; the env blocks are plain literals there.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

TURNSTILE_SECRET_NAME = "monolith-public-turnstile"


def _values_path() -> Path:
    srcdir = os.environ.get("TEST_SRCDIR", "")
    candidate = (
        Path(srcdir)
        / "_main"
        / "projects"
        / "monolith-public"
        / "chart"
        / "values.yaml"
    )
    if candidate.exists():
        return candidate
    # Fallback for a direct (non-bazel) run from the repo root.
    here = (
        Path(__file__).resolve().parent.parent
        / "monolith-public"
        / "chart"
        / "values.yaml"
    )
    if here.exists():
        return here
    raise FileNotFoundError(
        f"values.yaml not found at {candidate} or {here} (TEST_SRCDIR={srcdir!r})"
    )


def _load_values() -> dict:
    return yaml.safe_load(_values_path().read_text())


def _env_by_name(component: dict) -> dict:
    return {entry["name"]: entry for entry in component.get("env", [])}


def test_backend_references_turnstile_secret_via_secretkeyref():
    """The "web" backend gets SECRET_KEY + IP_HASH_SALT from the synced Secret."""
    values = _load_values()
    web_env = _env_by_name(values["web"])

    for var, key in (
        ("TURNSTILE_SECRET_KEY", "SECRET_KEY"),
        ("CHAT_PUBLIC_IP_HASH_SALT", "IP_HASH_SALT"),
    ):
        assert var in web_env, f"{var} missing from web.env"
        ref = web_env[var].get("valueFrom", {}).get("secretKeyRef", {})
        assert ref.get("name") == TURNSTILE_SECRET_NAME, f"{var} wrong secret"
        assert ref.get("key") == key, f"{var} wrong secret key"

    # The backend must NOT carry the public site key (it does not need it).
    assert "TURNSTILE_SITE_KEY" not in web_env


def test_frontend_carries_only_the_public_site_key_literal():
    """The SSR frontend gets the public site key as a literal, never the secret."""
    values = _load_values()
    frontend_env = _env_by_name(values["frontend"])

    # The site key is present as a plain literal value (no valueFrom).
    assert "TURNSTILE_SITE_KEY" in frontend_env
    site_key_entry = frontend_env["TURNSTILE_SITE_KEY"]
    assert "valueFrom" not in site_key_entry, "site key must be a literal"
    assert isinstance(site_key_entry.get("value"), str)
    assert site_key_entry["value"], "site key literal must be non-empty"

    # No frontend env entry may reference the Turnstile secret object, and the
    # backend-only secret/salt vars must not appear in the frontend at all.
    for name, entry in frontend_env.items():
        ref = entry.get("valueFrom", {}).get("secretKeyRef", {})
        assert ref.get("name") != TURNSTILE_SECRET_NAME, (
            f"frontend env {name} references the Turnstile secret"
        )
    assert "TURNSTILE_SECRET_KEY" not in frontend_env
    assert "CHAT_PUBLIC_IP_HASH_SALT" not in frontend_env


def test_site_key_literal_matches_the_values_ssot():
    """The frontend literal mirrors turnstile.siteKey (the documented SSOT)."""
    values = _load_values()
    frontend_env = _env_by_name(values["frontend"])
    assert frontend_env["TURNSTILE_SITE_KEY"]["value"] == values["turnstile"]["siteKey"]
