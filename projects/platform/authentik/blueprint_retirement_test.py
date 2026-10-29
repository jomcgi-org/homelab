"""Packaged production blueprints survive retirement of the unused dev client."""

import os
from pathlib import Path
import subprocess

import yaml


def test_rendered_blueprints_keep_all_production_inputs():
    chart = Path(__file__).resolve().parent
    result = subprocess.run(
        [os.environ.get("HELM_BIN", "helm"), "template", "authentik", str(chart),
         "--namespace", "authentik", "--show-only", "templates/blueprints-configmap.yaml"],
        capture_output=True, text=True, check=True,
    )
    documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert len(documents) == 1
    configmap = documents[0]
    assert configmap["kind"] == "ConfigMap"
    assert configmap["metadata"]["name"] == "authentik-mcp-blueprints"
    packaged = configmap["data"]
    assert set(packaged) == {
        "grimoire-auth.yaml", "kargo-auth.yaml", "mcp-auth.yaml",
        "moving-auth.yaml", "preview-auth.yaml",
    }
    # Default-enabled management claims are inserted by the packaging template.
    # Compare every other byte, including custom YAML tags, without constructing
    # provider objects or performing any account or Secret operation.
    for name, source in packaged.items():
        expected = (chart / "blueprints" / name).read_text()
        if name == "grimoire-auth.yaml":
            expected = expected.replace("        # PLATFORM_PROFILE_MAPPING", "        - !KeyOf platform-profile")
            expected = expected.replace("entries:\n", "entries:\n" + (chart / "optional/platform-profile.yaml").read_text() + "\n")
        if name == "mcp-auth.yaml":
            expected = expected.replace("        # PLATFORM_MANAGEMENT_PROFILE", "        - !KeyOf platform-management-profile")
            expected = expected.replace("entries:\n", "entries:\n" + (chart / "optional/platform-management-profile.yaml").read_text() + "\n")
        assert source.rstrip() == expected.rstrip()
        assert "DEV_OIDC_CLIENT_SECRET" not in source
        assert "dev.jomcgi.dev" not in source
