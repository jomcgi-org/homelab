"""Check a YAML stream of rendered factory manifests or kubectl get pods -A -o yaml."""
# invariant: PLAT-11

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
    if doc.get("kind") == "ConfigMap" and "envoy-gateway.yaml" in doc.get("data", {}):
        config = yaml.safe_load(doc["data"]["envoy-gateway.yaml"])
        if config and config.get("kind") == "EnvoyGateway":
            provider = config.get("provider", {}).get("kubernetes", {})
            for role, container in (
                ("shutdownManager", provider.get("shutdownManager", {})),
                (
                    "rateLimitDeployment",
                    provider.get("rateLimitDeployment", {}).get("container", {}),
                ),
            ):
                image = container.get("image", "")
                if not DIGEST.search(image):
                    yield f"{namespace}/{name}: {role}: {image or 'missing image'}"
    spec = doc.get("spec", {})
    if (
        doc.get("apiVersion") == "postgresql.cnpg.io/v1"
        and doc.get("kind") == "Cluster"
        and not spec.get("imageName")
    ):
        yield f"{namespace}/{name}: missing spec.imageName"
    if (
        doc.get("apiVersion") == "gateway.envoyproxy.io/v1alpha1"
        and doc.get("kind") == "EnvoyProxy"
        and spec.get("provider", {}).get("type") == "Kubernetes"
    ):
        provider = spec["provider"].get("kubernetes", {})
        workload = provider.get("envoyDaemonSet", provider.get("envoyDeployment", {}))
        image = workload.get("container", {}).get("image", "")
        if not DIGEST.search(image):
            yield f"{namespace}/{name}: EnvoyProxy container: {image or 'missing image'}"
    for image in images(spec):
        if not DIGEST.search(image):
            yield f"{namespace}/{name}: {image}"


if __name__ == "__main__":
    failures = [
        msg for doc in yaml.safe_load_all(sys.stdin) if doc for msg in violations(doc)
    ]
    for failure in failures:
        print(failure)
    sys.exit(bool(failures))
