"""The Grimoire provider issues refresh tokens the gateway can renew with."""

from pathlib import Path

import yaml

BLUEPRINT = Path(__file__).resolve().parent / "blueprints/grimoire-auth.yaml"
OFFLINE_ACCESS = [
    "authentik_providers_oauth2.scopemapping",
    ["managed", "goauthentik.io/providers/oauth2/scope-offline_access"],
]


def test_provider_maps_offline_access_and_keeps_its_refresh_token():
    # BaseLoader keeps !Find as its plain [model, [field, value]] list.
    entries = yaml.load(BLUEPRINT.read_text(), Loader=yaml.BaseLoader)["entries"]
    provider = next(
        entry["attrs"]
        for entry in entries
        if entry["model"] == "authentik_providers_oauth2.oauth2provider"
        and entry["identifiers"]["name"] == "grimoire"
    )
    assert "refresh_token" in provider["grant_types"]
    # authentik drops a requested scope that has no mapping on the provider.
    assert OFFLINE_ACCESS in provider["property_mappings"]
    # Concurrent renewals with one refresh token must not revoke each other.
    assert provider["refresh_token_threshold"] == "days=1"
