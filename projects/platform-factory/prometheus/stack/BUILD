load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "rollout_test",
    srcs = ["rollout_test.py"],
    data = [
        "alertmanager.yml",
        "kustomization.yaml",
        "//projects/platform-factory/prometheus/resources:kustomize_base",
        "//projects/platform-factory/prometheus/resources/alert-relay/chart",
        "//projects/platform-factory/prometheus/resources/alert-relay/deploy:application.yaml",
        "@multitool//tools/helm",
        "@multitool//tools/kustomize",
    ],
    env = {
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
        "KUSTOMIZE_BIN": "$(rootpath @multitool//tools/kustomize)",
    },
    deps = ["@pip//pyyaml"],
)
