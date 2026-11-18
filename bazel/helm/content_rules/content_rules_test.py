"""Each content rule passes a good manifest and fails a bad one (HOME-350)."""

import copy
import hashlib
import json

import pytest

from content_rules import check

NS = "app"
DIGEST = "ghcr.io/jomcgi/app@sha256:" + "0" * 64


def container(**over):
    c = {
        "name": "app",
        "image": DIGEST,
        "securityContext": {
            "runAsNonRoot": True,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "resources": {
            "requests": {"cpu": "10m", "memory": "32Mi"},
            "limits": {"memory": "64Mi"},
        },
    }
    c.update(over)
    return c


def deployment(pod=None, meta=None):
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "app", "namespace": NS},
        "spec": {
            "template": {
                "metadata": meta or {},
                "spec": pod or {"containers": [container()]},
            }
        },
    }


def sc(**over):
    s = copy.deepcopy(container()["securityContext"])
    s.update(over)
    return s


def test_good_deployment_passes():
    assert check([deployment()], NS) == []


def test_cronjob_pod_is_checked():
    cron = {
        "kind": "CronJob",
        "metadata": {"name": "c"},
        "spec": {
            "jobTemplate": {
                "spec": {"template": {"spec": {"containers": [container(image="x:1")]}}}
            }
        },
    }
    assert any("names no digest" in p for p in check([cron], NS))


@pytest.mark.parametrize(
    "doc,expect",
    [
        (deployment({"containers": [container(image="nginx:1")]}), "names no digest"),
        (
            deployment(
                {
                    "initContainers": [container(image="busybox")],
                    "containers": [container()],
                }
            ),
            "names no digest",
        ),
        ({"kind": "ClusterRole", "metadata": {"name": "r"}}, "cluster-scoped"),
        ({"kind": "Namespace", "metadata": {"name": "other"}}, "cluster-scoped"),
        (
            {
                "kind": "ConfigMap",
                "metadata": {"name": "c", "namespace": "kube-system"},
            },
            "not the release namespace",
        ),
        (
            deployment({"hostNetwork": True, "containers": [container()]}),
            "sets hostNetwork",
        ),
        (deployment({"hostPID": True, "containers": [container()]}), "sets hostPID"),
        (
            deployment(
                {
                    "containers": [container()],
                    "volumes": [{"name": "h", "hostPath": {"path": "/"}}],
                }
            ),
            "hostPath",
        ),
        (
            deployment(
                {"containers": [container(securityContext=sc(privileged=True))]}
            ),
            "privileged",
        ),
        (
            {
                "kind": "Service",
                "metadata": {"name": "s"},
                "spec": {"type": "NodePort"},
            },
            "node address",
        ),
        (
            {
                "kind": "Service",
                "metadata": {"name": "s"},
                "spec": {"externalIPs": ["1.2.3.4"]},
            },
            "externalIPs",
        ),
        (
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": "r"},
                "rules": [
                    {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]}
                ],
            },
            "every Secret",
        ),
        (
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": "r"},
                "rules": [
                    {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["create"]}
                ],
            },
            "creates workloads",
        ),
        (
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": "r"},
                "rules": [
                    {
                        "apiGroups": ["rbac.authorization.k8s.io"],
                        "resources": ["rolebindings"],
                        "verbs": ["patch"],
                    }
                ],
            },
            "writes RBAC",
        ),
        (
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": "r"},
                "rules": [{"apiGroups": [""], "resources": ["*"], "verbs": ["get"]}],
            },
            "wildcard",
        ),
        (
            deployment(
                {"containers": [container(securityContext=sc(runAsNonRoot=False))]}
            ),
            "runAsNonRoot",
        ),
        (
            deployment(
                {
                    "containers": [
                        container(securityContext=sc(allowPrivilegeEscalation=True))
                    ]
                }
            ),
            "privilege escalation",
        ),
        (
            deployment(
                {
                    "containers": [
                        container(
                            securityContext=sc(capabilities={"drop": ["NET_RAW"]})
                        )
                    ]
                }
            ),
            "drop all capabilities",
        ),
        (
            deployment(
                {
                    "containers": [
                        container(
                            securityContext=sc(seccompProfile={"type": "Unconfined"})
                        )
                    ]
                }
            ),
            "seccomp",
        ),
        (
            deployment(
                {
                    "containers": [
                        container(securityContext=sc(readOnlyRootFilesystem=False))
                    ]
                }
            ),
            "writable root filesystem",
        ),
        (
            deployment(
                {"containers": [container(resources={"requests": {"cpu": "1m"}})]}
            ),
            "memory requests",
        ),
    ],
)
def test_bad_manifest_fails(doc, expect):
    problems = check([doc], NS)
    assert any(expect in p for p in problems), problems


def test_named_secret_read_passes():
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": "r", "namespace": NS},
        "rules": [
            {
                "apiGroups": [""],
                "resources": ["secrets"],
                "resourceNames": ["one"],
                "verbs": ["get"],
            }
        ],
    }
    assert check([role], NS) == []


def test_clusterip_service_passes():
    assert (
        check(
            [
                {
                    "kind": "Service",
                    "metadata": {"name": "s"},
                    "spec": {"type": "ClusterIP"},
                }
            ],
            NS,
        )
        == []
    )


def test_writable_root_passes_with_exception():
    meta = {
        "annotations": {
            "factory.jomcgi.dev/exceptions": json.dumps(
                [
                    {
                        "workload": "app",
                        "default": "readOnlyRootFilesystem",
                        "reason": "r",
                    }
                ]
            )
        }
    }
    pod = {"containers": [container(securityContext=sc(readOnlyRootFilesystem=False))]}
    assert check([deployment(pod, meta)], NS) == []


def test_pod_level_security_context_counts():
    s = sc()
    del s["runAsNonRoot"], s["seccompProfile"]
    pod = {
        "securityContext": {
            "runAsNonRoot": True,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [container(securityContext=s)],
    }
    assert check([deployment(pod)], NS) == []


def test_empty_and_kindless_documents_are_skipped():
    assert check([None, {}, {"metadata": {}}], NS) == []


def policy(expression=f'request.namespace == "{NS}"'):
    return {
        "kind": "ValidatingAdmissionPolicy",
        "metadata": {"name": f"{NS}-guard"},
        "spec": {"matchConditions": [{"name": "ns", "expression": expression}]},
    }


def binding(**spec):
    return {
        "kind": "ValidatingAdmissionPolicyBinding",
        "metadata": {"name": f"{NS}-guard"},
        "spec": {"policyName": f"{NS}-guard", "validationActions": ["Deny"], **spec},
    }


LABEL = {"factory.jomcgi.dev/garage-claim": "true"}


def claim(labels, namespace=NS, secret="bucket", name=None):
    digest = hashlib.sha256(f"{namespace}/{secret}".encode()).hexdigest()
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": name or f"garage-claim-{digest}",
            "namespace": "garage",
            "labels": labels,
        },
        "data": {
            "namespace": namespace,
            "secretName": secret,
            "bucket": "app-data",
            "access": "read",
        },
    }


def test_library_policy_scoped_to_release_namespace_passes():
    assert check([policy(), binding()], NS) == []


def test_policy_scoped_to_another_namespace_fails():
    problems = check([policy('request.namespace == "other"'), binding()], NS)
    assert sum("cluster-scoped" in p for p in problems) == 2, problems


def test_binding_that_widens_its_match_fails():
    problems = check([policy(), binding(matchResources={"namespaceSelector": {}})], NS)
    assert any("ValidatingAdmissionPolicyBinding" in p for p in problems), problems


def test_labelled_garage_claim_passes():
    assert check([claim(LABEL)], NS) == []


def test_garage_claim_for_another_namespace_fails():
    assert any(
        "not the release namespace" in p
        for p in check([claim(LABEL, namespace="other")], NS)
    )


def test_garage_claim_with_wrong_name_fails():
    assert any(
        "not the release namespace" in p
        for p in check([claim(LABEL, name="garage-claim-x")], NS)
    )


def test_policy_with_a_platform_name_fails():
    doc = policy()
    doc["metadata"]["name"] = "garage-claim-authorizations"
    assert any("cluster-scoped" in p for p in check([doc], NS))


def test_unclassified_kind_fails():
    doc = {
        "apiVersion": "storage.k8s.io/v1",
        "kind": "VolumeAttachment",
        "metadata": {"name": "v"},
    }
    assert any("unclassified" in p for p in check([doc], NS))


def test_wildcard_verb_fails():
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": "r"},
        "rules": [{"apiGroups": [""], "resources": ["configmaps"], "verbs": ["*"]}],
    }
    assert any("wildcard" in p for p in check([role], NS))


def test_empty_digest_fails():
    doc = deployment({"containers": [container(image="ghcr.io/x@sha256:")]})
    assert any("names no digest" in p for p in check([doc], NS))


def test_added_capability_fails():
    s = sc(capabilities={"drop": ["ALL"], "add": ["NET_ADMIN"]})
    doc = deployment({"containers": [container(securityContext=s)]})
    assert any("adds capabilities" in p for p in check([doc], NS))


def test_unlabelled_garage_configmap_fails():
    assert any("not the release namespace" in p for p in check([claim({})], NS))


def test_exemption_allows_the_named_object_only():
    svc = {"kind": "Service", "metadata": {"name": "status", "namespace": "factory"}}
    other = {"kind": "Service", "metadata": {"name": "other", "namespace": "factory"}}
    problems = check([svc, other], NS, ["Service/factory/status"])
    assert problems == [
        f"Service/other: namespace factory is not the release namespace {NS}"
    ]


def test_unused_exemption_fails():
    problems = check([deployment()], NS, ["Service/factory/gone"])
    assert any("matches no object" in p for p in problems), problems


def test_crd_fails():
    crd = {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {"name": "things.example.com"},
    }
    assert any("cluster-scoped" in p for p in check([crd], NS))


def test_exemption_never_covers_a_cluster_scoped_kind():
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRole",
        "metadata": {"name": "r"},
    }
    problems = check([role], NS, ["ClusterRole//r"])
    assert any("cluster-scoped" in p for p in problems), problems


@pytest.mark.parametrize("access", ["admin", ""])
def test_garage_claim_with_unsupported_access_fails(access):
    doc = claim(LABEL)
    doc["data"]["access"] = access
    assert any("not the release namespace" in p for p in check([doc], NS))


def test_garage_claim_with_invalid_bucket_fails():
    doc = claim(LABEL)
    doc["data"]["bucket"] = "B"
    assert any("not the release namespace" in p for p in check([doc], NS))


@pytest.mark.parametrize("verb", ["bind", "escalate", "impersonate"])
def test_escalation_verbs_fail(verb):
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": "r"},
        "rules": [
            {
                "apiGroups": ["rbac.authorization.k8s.io"],
                "resources": ["roles"],
                "verbs": [verb],
            }
        ],
    }
    assert any(verb in p for p in check([role], NS))


def test_replicationcontroller_create_fails():
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": "r"},
        "rules": [
            {
                "apiGroups": [""],
                "resources": ["replicationcontrollers"],
                "verbs": ["create"],
            }
        ],
    }
    assert any("creates workloads" in p for p in check([role], NS))
