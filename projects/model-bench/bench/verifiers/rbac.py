from pathlib import Path

import yaml

from bench.verifiers import register, VerifyResult


def _rule_covers(rule: dict, group: str, resource: str, verb: str) -> bool:
    """Return True if a single RBAC rule covers (group, resource, verb), honoring '*' wildcards."""
    api_groups = rule.get("apiGroups", [])
    resources = rule.get("resources", [])
    verbs = rule.get("verbs", [])

    group_ok = "*" in api_groups or group in api_groups
    resource_ok = "*" in resources or resource in resources
    verb_ok = "*" in verbs or verb in verbs

    return group_ok and resource_ok and verb_ok


def _is_covered(rules: list[dict], group: str, resource: str, verb: str) -> bool:
    """Return True if any rule in the list covers the given (group, resource, verb)."""
    return any(_rule_covers(r, group, resource, verb) for r in rules)


@register("rbac-cover")
def verify(workdir: Path, args: dict) -> VerifyResult:
    """Assert that a ClusterRole YAML covers all required (group, resource, verb) triples.

    args:
        clusterrole: path to the ClusterRole YAML, relative to workdir
        required: list of {group, resource, verb} dicts that must all be covered
    """
    role_path = workdir / args["clusterrole"]
    try:
        with open(role_path) as f:
            doc = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        # A malformed ClusterRole is a graded failure, not a harness crash, so the
        # model gets the parse error as feedback and can fix it on the retry.
        return VerifyResult(False, f"clusterrole.yaml is not valid YAML: {exc}")

    if not isinstance(doc, dict):
        return VerifyResult(
            False,
            "clusterrole.yaml must be a YAML mapping with a 'rules' list, "
            f"got {type(doc).__name__}",
        )

    rules = doc.get("rules", [])
    if not isinstance(rules, list):
        return VerifyResult(False, "clusterrole.yaml 'rules' must be a list")

    missing = []
    for req in args["required"]:
        group = req["group"]
        resource = req["resource"]
        verb = req["verb"]
        if not _is_covered(rules, group, resource, verb):
            missing.append(f"{group}/{resource}:{verb}")

    if missing:
        items = ", ".join(missing)
        return VerifyResult(False, f"ClusterRole is missing coverage for: {items}")
    return VerifyResult(True, "")
