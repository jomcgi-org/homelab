"""Compare Renovate's local JSON extraction log with the checked-out factory pins."""

import json
import re
import sys
from pathlib import Path

import yaml


def check(log):
    extracted = {}
    for line in log:
        if not line.startswith("{"):
            continue
        for files in json.loads(line).get("packageFiles", {}).values():
            for file in files:
                for dep in file["deps"]:
                    extracted[file["packageFile"], dep["depName"]] = dep

    root = Path("projects/platform-factory")
    substrate = root / "substrate"
    script = str(substrate / "render-upstream.sh")
    release = extracted[script, "ghcr.io/kagent-dev/substrate/helm/substrate"]
    assert release["packageName"] == "kagent-dev/substrate"
    assert release["datasource"] == "github-releases"
    count = 0
    for path in sorted(root.rglob("*")):
        if path.suffix not in (".yaml", ".yml"):
            continue
        content = path.read_text()
        pins = re.findall(
            r"""(?m)^\s*(?:-\s+)?(?:image|imageName|reference):[ \t]*["']?([\w./-]+):([\w.-]+)(?:@(sha256:[\da-f]{64}))?""",
            content,
        )
        if path.name == "kustomization.yaml":
            for pin in (yaml.safe_load(content) or {}).get("images", []):
                pins.append((pin["name"], pin["newTag"], pin["digest"]))
        for name, version, digest in pins:
            owner = str(path)
            if path.is_relative_to(substrate / "resources/upstream"):
                if name.startswith("ghcr.io/kagent-dev/substrate/"):
                    assert version == "v" + release["currentValue"], (path, name)
                    print(path, name, "via", script)
                    count += 1
                    continue
                owner = str(substrate / "resources/kustomization.yaml")
            dep = extracted[owner, name]
            assert dep["datasource"] == "docker", (owner, name)
            if owner == str(path):
                assert dep["currentValue"] == version, (path, name, version)
                assert (dep.get("currentDigest") or "") == digest, (path, name, digest)
            print(path, name, "via", owner)
            count += 1
    assert count, "empty factory image inventory"


if __name__ == "__main__":
    check(sys.stdin)
