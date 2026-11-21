"""Read rendered Cilium policies on stdin and print a local DNS coverage inventory."""

import argparse
import re
import sys

import yaml


def applies(selector, workload):
    if selector == workload:
        return True
    labels = workload.get("matchLabels", {})
    return set(selector) <= {"matchLabels"} and all(
        labels.get(key) == value
        for key, value in selector.get("matchLabels", {}).items()
    )


def inventory(documents, default_namespace):
    declarations = set()
    dns = []
    for doc in documents:
        if not doc or doc.get("kind") not in {
            "CiliumNetworkPolicy",
            "CiliumClusterwideNetworkPolicy",
        }:
            continue
        meta = doc["metadata"]
        namespace = (
            meta.get("namespace", default_namespace)
            if doc["kind"] == "CiliumNetworkPolicy"
            else "*"
        )
        for spec in doc.get("specs", []) + ([doc["spec"]] if "spec" in doc else []):
            selector = spec.get("endpointSelector")
            if selector is None:
                continue
            for rule in spec.get("egress", []):
                for fqdn in rule.get("toFQDNs", []):
                    name = (
                        (fqdn.get("matchName") or fqdn["matchPattern"])
                        .lower()
                        .rstrip(".")
                    )
                    if name != "svc.cluster.local" and not name.endswith(
                        ".svc.cluster.local"
                    ):
                        declarations.add(
                            (namespace, meta["name"], yaml.safe_dump(selector), name)
                        )
                for port in rule.get("toPorts", []):
                    if any(str(p["port"]) == "53" for p in port.get("ports", [])):
                        for entry in port.get("rules", {}).get("dns", []):
                            dns.append((namespace, selector, entry))

    for namespace, policy, serialized, name in sorted(declarations):
        workload = yaml.safe_load(serialized)
        status = "missing"
        for scope, selector, entry in dns:
            if scope not in {"*", namespace} or not applies(selector, workload):
                continue
            if entry.get("matchName", "").lower().rstrip(".") == name:
                status = "explicit"
                break
            pattern = entry.get("matchPattern", "").lower().rstrip(".")
            if pattern == name and "*" not in name:
                status = "explicit"
                break
            if (
                pattern == "*"
                or pattern == name
                or (
                    "*" not in name
                    and re.fullmatch(re.escape(pattern).replace(r"\*", "[^.]*"), name)
                )
            ):
                status = "wildcard-only"
        yield namespace, policy, name, status


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--namespace", default="default", help="namespace for unqualified policies"
    )
    args = parser.parse_args()
    incomplete = False
    for row in inventory(yaml.safe_load_all(sys.stdin), args.namespace):
        print(*row, sep="\t")
        incomplete |= row[-1] != "explicit"
    sys.exit(int(incomplete))
