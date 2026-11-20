"""Check a YAML stream of rendered factory manifests or kubectl get pods -A -o yaml."""

import re
import sys

import yaml

DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
K3S_ADDONS = {"coredns", "local-path-provisioner", "metrics-server", "traefik"}


def images(node):
    if isinstance(node, list):
        for item in node:
            yield from images(item)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in {
                "containers",
                "initContainers",
                "ephemeralContainers",
            } and isinstance(value, list):
                for container in value:
                    yield container.get("image", "")
            elif key == "imageName" and isinstance(value, str):
                yield value
            else:
                yield from images(value)


def violations(doc):
    if doc.get("kind") in {"List", "PodList"}:
        for item in doc["items"]:
            yield from violations(item)
        return
    meta = doc.get("metadata", {})
    namespace = meta.get("namespace", "")
    name = meta.get("name", "")
    if namespace == "kube-system" and any(
        name == addon or name.startswith(addon + "-") for addon in K3S_ADDONS
    ):
        return
    for image in images(doc.get("spec", {})):
        if not DIGEST.search(image):
            yield f"{namespace}/{name}: {image}"


if __name__ == "__main__":
    failures = [
        msg for doc in yaml.safe_load_all(sys.stdin) if doc for msg in violations(doc)
    ]
    for failure in failures:
        print(failure)
    sys.exit(bool(failures))
