# The test target lists both sources; gazelle would add a binary for the
# script's __main__ block with no main attribute.
# gazelle:exclude check_image_digests.py

load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "platform_images_test",
    srcs = ["platform_images_test.py"],
    # Collect only this file: the chart data carries other charts' tests.
    args = ["$(rootpath platform_images_test.py)"],
    data = [
        "//projects:platform-factory/authentik/application.yaml",
        "//projects:platform-factory/cert-manager/application.yaml",
        "//projects:platform-factory/edge/application.yaml",
        "//projects:platform-factory/kargo/application.yaml",
        "//projects/platform-factory/argocd:application.yaml",
        "//projects/platform-factory/edge/chart",
        "//projects/platform-factory/factory-db/resources:cluster.yaml",
        "//projects/platform-factory/grimoire-data/resources:database.yaml",
        "//projects/platform-factory/kagent-postgres/resources:cluster.yaml",
        "//projects/platform-factory/onepassword:application.yaml",
        "//projects/platform-factory/otel-collector:application.yaml",
        "//projects/platform-factory/renovate:application.yaml",
        "//projects/platform/argocd:chart",
        "//projects/platform/authentik:chart",
        "//projects/platform/cert-manager:chart",
        "//projects/platform/kargo:chart",
        "//projects/platform/onepassword:chart",
        "//projects/platform/otel-collector:chart",
        "//projects/platform/renovate:chart",
        "@multitool//tools/helm",
    ],
    env = {"HELM_BIN": "$(rootpath @multitool//tools/helm)"},
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
