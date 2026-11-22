load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "policies_test",
    srcs = ["policies_test.py"],
    # The chart targets carry their own Python tests; collect only this file.
    args = ["$(rootpath policies_test.py)"],
    data = [
        "//projects:platform-factory/cert-manager/resources/network-policy.yaml",
        "//projects:platform-factory/cloudnative-pg/resources/network-policy.yaml",
        "//projects:platform-factory/kargo/resources/network-policy.yaml",
        "//projects/factory-explore/chart",
        "//projects/factory/chart",
        "//projects/platform-factory/authentik/resources:network-policy.yaml",
        "//projects/platform-factory/cfgate/resources:network-policy.yaml",
        "//projects/platform-factory/edge/chart",
        "//projects/platform-factory/external-secrets/chart",
        "//projects/platform-factory/factory-db/resources:network-policy.yaml",
        "//projects/platform-factory/garage/resources:network-policy.yaml",
        "//projects/platform-factory/grafana/resources:network-policy.yaml",
        "//projects/platform-factory/grimoire-data/resources:policy.yaml",
        "//projects/platform-factory/hindsight-discord/resources:network-policy.yaml",
        "//projects/platform-factory/hindsight/resources:network-policy.yaml",
        "//projects/platform-factory/kagent-postgres/resources:network-policy.yaml",
        "//projects/platform-factory/network-policy:resources/argocd.yaml",
        "//projects/platform-factory/network-policy:resources/kube-system.yaml",
        "//projects/platform-factory/network-policy:resources/onepassword.yaml",
        "//projects/platform-factory/otel-collector:resources/network-policy.yaml",
        "//projects/platform-factory/prometheus/resources:network-policy.yaml",
        "//projects/platform-factory/substrate/resources:network-policy.yaml",
        "//projects/ships/chart",
        "@multitool//tools/helm",
    ],
    env = {"HELM_BIN": "$(rootpath @multitool//tools/helm)"},
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
