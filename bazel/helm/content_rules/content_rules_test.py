"""Each content rule passes a good manifest and fails a bad one (HOME-350)."""

import copy
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
        "metadata": {"name": "guard"},
        "spec": {"matchConditions": [{"name": "ns", "expression": expression}]},
    }


def binding(**spec):
    return {
        "kind": "ValidatingAdmissionPolicyBinding",
        "metadata": {"name": "guard"},
        "spec": {"policyName": "guard", "validationActions": ["Deny"], **spec},
    }


def claim(labels):
    return {
        "kind": "ConfigMap",
        "metadata": {"name": "garage-claim-x", "namespace": "garage", "labels": labels},
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
    assert check([claim({"factory.jomcgi.dev/garage-claim": "true"})], NS) == []


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
