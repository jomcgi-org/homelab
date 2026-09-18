load("//bazel/helm:defs.bzl", "argocd_app")

filegroup(
    name = "gke_values",
    srcs = ["values-gke.yaml"],
    visibility = ["//projects/inference/chart:__pkg__"],
)

argocd_app(
    name = "inference",
    chart = "projects/inference/chart",
    chart_files = "//projects/inference/chart:chart",
    namespace = "inference",
    release_name = "inference",
    tags = [
        "helm",
        "template",
    ],
    values_files = ["//projects/inference/chart:values.yaml"],
)
