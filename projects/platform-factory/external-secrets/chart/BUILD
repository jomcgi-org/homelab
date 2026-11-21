load("//bazel/helm:defs.bzl", "helm_chart")
load("//bazel/tools/pytest:defs.bzl", "py_test")

helm_chart(
    name = "chart",
    visibility = ["//visibility:public"],
)

py_test(
    name = "external_secrets_test",
    srcs = ["external_secrets_test.py"],
    data = [
        ":chart",
        "//projects/platform-factory/external-secrets:application.yaml",
        "//projects/platform-factory/external-secrets:values.yaml",
        "//projects/platform-factory/network-policy:resources/argocd.yaml",
        "@multitool//tools/helm",
    ],
    env = {
        "ESO_APPLICATION": "$(rootpath //projects/platform-factory/external-secrets:application.yaml)",
        "ESO_ARGOCD_NETWORK_POLICY": "$(rootpath //projects/platform-factory/network-policy:resources/argocd.yaml)",
        "ESO_VALUES": "$(rootpath //projects/platform-factory/external-secrets:values.yaml)",
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
    },
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
