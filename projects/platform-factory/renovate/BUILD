load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "renovate",
    chart = "projects/platform/renovate",
    chart_files = "//projects/platform/renovate:chart",
    namespace = "renovate",
    release_name = "renovate",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/platform/renovate:values.yaml",
        "//projects/platform/renovate:values-factory.yaml",
    ],
)

filegroup(
    name = "manifests",
    srcs = [
        "application.yaml",
        "kustomization.yaml",
        "resources/kustomization.yaml",
        "resources/network-policy.yaml",
    ],
    visibility = ["//projects/platform/renovate:__pkg__"],
)

exports_files(
    ["application.yaml"],
    visibility = ["//projects/platform-factory/image-audit:__pkg__"],
)
