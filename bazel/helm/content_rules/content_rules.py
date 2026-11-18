#!/usr/bin/env python3
"""Content rules for application charts on the factory cluster (HOME-350).

Usage: content_rules.py RELEASE_NAMESPACE [Kind/namespace/name ...] < render.yaml

Reads a `helm template` render on stdin and prints one line for each broken
rule. Exit 1 when a rule breaks. Each rule is a pure function from one
manifest to a list of messages, so the test proves it with synthetic input.

Each extra argument exempts one object from the namespace rule. The app's
deploy/BUILD lists it with a reason (content_rules_exceptions). An exemption
that matches no object fails, so the list only shrinks.
"""

import hashlib
import json
import re
import sys

import yaml

# Namespaced kinds an application chart may render, by API group. Every
# other kind fails as unclassified: it is cluster-scoped or not yet reviewed.
# Add a kind here only after checking that the API server namespaces it.
NAMESPACED = {
    "": {
        "ConfigMap",
        "Endpoints",
        "LimitRange",
        "PersistentVolumeClaim",
        "Pod",
        "ResourceQuota",
        "Secret",
        "Service",
        "ServiceAccount",
    },
    "apps": {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"},
    "batch": {"Job", "CronJob"},
    "autoscaling": {"HorizontalPodAutoscaler"},
    "policy": {"PodDisruptionBudget"},
    "networking.k8s.io": {"NetworkPolicy", "Ingress"},
    "rbac.authorization.k8s.io": {"Role", "RoleBinding"},
    "cilium.io": {"CiliumNetworkPolicy"},
    "gateway.networking.k8s.io": {"HTTPRoute", "GRPCRoute", "ReferenceGrant"},
    "gateway.envoyproxy.io": {
        "BackendTrafficPolicy",
        "HTTPRouteFilter",
        "SecurityPolicy",
    },
    "external-secrets.io": {"ExternalSecret", "SecretStore"},
    "generators.external-secrets.io": {"GithubAccessToken"},
    "onepassword.com": {"OnePasswordItem"},
    "cert-manager.io": {"Certificate", "Issuer"},
    "monitoring.coreos.com": {"PodMonitor", "PrometheusRule", "ServiceMonitor"},
    "postgresql.cnpg.io": {"Cluster", "ScheduledBackup"},
}


def api_group(doc):
    version = str(doc.get("apiVersion") or "")
    return version.split("/", 1)[0] if "/" in version else ""


def namespaced(doc):
    return doc.get("kind") in NAMESPACED.get(api_group(doc), set())


WORKLOAD_RESOURCES = {
    "pods",
    "deployments",
    "statefulsets",
    "daemonsets",
    "replicasets",
    "jobs",
    "cronjobs",
    "replicationcontrollers",
}
WRITE_VERBS = {"create", "update", "patch", "delete", "deletecollection", "*"}
# bind and escalate skip RBAC's own privilege checks; impersonate acts as
# another identity. A Role in an application chart grants none of them.
ESCALATION_VERBS = {"bind", "escalate", "impersonate"}
EXCEPTIONS_ANNOTATION = "factory.jomcgi.dev/exceptions"
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def _name(doc):
    meta = doc.get("metadata") or {}
    return f"{doc.get('kind')}/{meta.get('name')}"


def pod_template(doc):
    """Return (pod metadata, pod spec) for a workload, or (None, None)."""
    kind = doc.get("kind")
    spec = doc.get("spec") or {}
    if kind == "Pod":
        return doc.get("metadata") or {}, spec
    if kind == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
    if kind in {
        "Deployment",
        "StatefulSet",
        "DaemonSet",
        "ReplicaSet",
        "Job",
        "CronJob",
    }:
        template = spec.get("template") or {}
        return template.get("metadata") or {}, template.get("spec") or {}
    return None, None


def containers(pod):
    for field in ("initContainers", "containers", "ephemeralContainers"):
        for c in pod.get(field) or []:
            yield c


def rule_digest(doc, namespace):
    _, pod = pod_template(doc)
    if pod is None:
        return []
    return [
        f"{_name(doc)}: container {c.get('name')} image {c.get('image')} names no digest"
        for c in containers(pod)
        if not DIGEST.search(str(c.get("image", "")))
    ]


GARAGE_CLAIM_LABEL = "factory.jomcgi.dev/garage-claim"
ADMISSION_POLICY = "ValidatingAdmissionPolicy"
ADMISSION_BINDING = "ValidatingAdmissionPolicyBinding"


def _key(doc):
    meta = doc.get("metadata") or {}
    return f"{doc.get('kind')}/{meta.get('namespace', '')}/{meta.get('name')}"


def scoped_policy(doc, namespace):
    """A library admission policy that matches only the release namespace.

    An admission policy can only refuse a request, so one confined to the
    app's own namespace cannot reach another workload. The library names it
    `<release namespace>-...`, so it cannot replace a platform policy.
    """
    if doc.get("kind") != ADMISSION_POLICY:
        return False
    name = str((doc.get("metadata") or {}).get("name") or "")
    if not name.startswith(f"{namespace}-"):
        return False
    want = f"request.namespace == {json.dumps(namespace)}"
    conditions = (doc.get("spec") or {}).get("matchConditions") or []
    return any(c.get("expression", "").strip() == want for c in conditions)


CLAIM_FIELDS = {"namespace", "secretName", "bucket", "access"}
CLAIM_ACCESS = {"read", "write", "readwrite"}
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
BUCKET = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")


def garage_claim(doc, namespace):
    """A library bucket claim for this release, as _buckets.tpl renders it.

    Garage grants the bucket to data.namespace, so the claim must name the
    release namespace and carry the library's deterministic name.
    """
    meta = doc.get("metadata") or {}
    data = doc.get("data") or {}
    if (meta.get("labels") or {}).get(GARAGE_CLAIM_LABEL) != "true":
        return False
    if set(data) != CLAIM_FIELDS or data.get("namespace") != namespace:
        return False
    # The same checks as discover_grants in
    # projects/platform-factory/garage/resources/garage_init.py: one invalid
    # claim stops Garage from granting every claim.
    if not all(DNS_LABEL.fullmatch(str(data[k])) for k in ("namespace", "secretName")):
        return False
    if not BUCKET.fullmatch(str(data["bucket"])) or data["access"] not in CLAIM_ACCESS:
        return False
    digest = hashlib.sha256(f"{namespace}/{data['secretName']}".encode()).hexdigest()
    return meta.get("name") == f"garage-claim-{digest}"


def library_owned(doc, namespace, policies):
    """Objects the factory library places outside the release namespace."""
    kind = doc.get("kind")
    meta = doc.get("metadata") or {}
    if kind == "ConfigMap" and meta.get("namespace") == "garage":
        return api_group(doc) == "" and garage_claim(doc, namespace)
    if kind == ADMISSION_POLICY:
        return scoped_policy(doc, namespace)
    if kind == ADMISSION_BINDING:
        spec = doc.get("spec") or {}
        return (
            spec.get("policyName") in policies
            and meta.get("name") == spec.get("policyName")
            and not spec.get("paramRef")
            and not spec.get("matchResources")
        )
    return False


def rule_namespace(doc, namespace, policies=frozenset()):
    if library_owned(doc, namespace, policies):
        return []
    if not namespaced(doc):
        return [
            f"{_name(doc)}: {doc.get('apiVersion')} {doc.get('kind')} is cluster-scoped "
            "or unclassified; an application chart renders only namespaced kinds"
        ]
    ns = (doc.get("metadata") or {}).get("namespace")
    if ns is not None and ns != namespace:
        return [
            f"{_name(doc)}: namespace {ns} is not the release namespace {namespace}"
        ]
    return []


def rule_host_access(doc, namespace):
    _, pod = pod_template(doc)
    if pod is None:
        return []
    out = []
    for field in ("hostNetwork", "hostPID", "hostIPC"):
        if pod.get(field):
            out.append(f"{_name(doc)}: sets {field}")
    for v in pod.get("volumes") or []:
        if "hostPath" in v:
            out.append(f"{_name(doc)}: volume {v.get('name')} is a hostPath")
    for c in containers(pod):
        if (c.get("securityContext") or {}).get("privileged"):
            out.append(f"{_name(doc)}: container {c.get('name')} is privileged")
    return out


def rule_service(doc, namespace):
    """No Service exposes a pod on a node address (PLAT-4)."""
    if doc.get("kind") != "Service":
        return []
    spec = doc.get("spec") or {}
    out = []
    if spec.get("type") in {"NodePort", "LoadBalancer"}:
        out.append(f"{_name(doc)}: type {spec['type']} exposes a node address")
    if spec.get("externalIPs"):
        out.append(f"{_name(doc)}: sets externalIPs")
    return out


def rule_role(doc, namespace):
    if doc.get("kind") != "Role":
        return []
    out = []
    for i, r in enumerate(doc.get("rules") or []):
        groups = set(r.get("apiGroups") or [])
        resources = set(r.get("resources") or [])
        verbs = set(r.get("verbs") or [])
        where = f"{_name(doc)}: rule {i}"
        if "*" in resources or "*" in groups or "*" in verbs:
            out.append(f"{where} uses a wildcard")
        if "secrets" in resources and not r.get("resourceNames"):
            out.append(f"{where} reaches every Secret; name them in resourceNames")
        if resources & WORKLOAD_RESOURCES and verbs & {"create", "*"}:
            out.append(f"{where} creates workloads")
        if "rbac.authorization.k8s.io" in groups and verbs & WRITE_VERBS:
            out.append(f"{where} writes RBAC")
        if verbs & ESCALATION_VERBS:
            out.append(f"{where} grants {sorted(verbs & ESCALATION_VERBS)}")
    return out


def _exceptions(meta):
    raw = ((meta or {}).get("annotations") or {}).get(EXCEPTIONS_ANNOTATION)
    if not raw:
        return set()
    return {e.get("default") for e in json.loads(raw)}


def rule_library_defaults(doc, namespace):
    """Every container meets the factory library's defaults (LIB-1, LIB-4).

    Only readOnlyRootFilesystem may be relaxed, and only when the pod
    template's exceptions annotation names it.
    """
    meta, pod = pod_template(doc)
    if pod is None:
        return []
    allowed = _exceptions(meta)
    psc = pod.get("securityContext") or {}
    out = []
    for c in containers(pod):
        sc = c.get("securityContext") or {}
        where = f"{_name(doc)}: container {c.get('name')}"
        if not (sc.get("runAsNonRoot", psc.get("runAsNonRoot")) is True):
            out.append(f"{where} does not set runAsNonRoot")
        if sc.get("allowPrivilegeEscalation") is not False:
            out.append(f"{where} allows privilege escalation")
        caps = sc.get("capabilities") or {}
        if "ALL" not in (caps.get("drop") or []):
            out.append(f"{where} does not drop all capabilities")
        if caps.get("add"):
            out.append(f"{where} adds capabilities {caps['add']}")
        seccomp = (sc.get("seccompProfile") or psc.get("seccompProfile") or {}).get(
            "type"
        )
        if seccomp != "RuntimeDefault":
            out.append(f"{where} does not use the RuntimeDefault seccomp profile")
        if (
            sc.get("readOnlyRootFilesystem") is not True
            and "readOnlyRootFilesystem" not in allowed
        ):
            out.append(f"{where} has a writable root filesystem without an exception")
        res = c.get("resources") or {}
        req = res.get("requests") or {}
        if (
            "cpu" not in req
            or "memory" not in req
            or "memory" not in (res.get("limits") or {})
        ):
            out.append(f"{where} lacks CPU and memory requests or a memory limit")
    return out


RULES = [
    rule_digest,
    rule_namespace,
    rule_host_access,
    rule_service,
    rule_role,
    rule_library_defaults,
]


def check(docs, namespace, exempt=()):
    docs = [d for d in docs if isinstance(d, dict) and d.get("kind")]
    policies = {
        (d.get("metadata") or {}).get("name")
        for d in docs
        if scoped_policy(d, namespace)
    }
    exempt = set(exempt)
    used = set()
    out = []
    for doc in docs:
        for rule in RULES:
            if rule is rule_namespace:
                found = rule(doc, namespace, policies)
                # An exemption covers a namespaced object in another
                # namespace, never a cluster-scoped or unclassified kind.
                if found and namespaced(doc) and _key(doc) in exempt:
                    used.add(_key(doc))
                    continue
            else:
                found = rule(doc, namespace)
            out.extend(found)
    for key in sorted(exempt - used):
        out.append(f"exemption {key} matches no object that needs it; remove it")
    return out


def main(argv):
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    problems = check(list(yaml.safe_load_all(sys.stdin)), argv[1], argv[2:])
    for p in problems:
        print(f"content rule: {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
