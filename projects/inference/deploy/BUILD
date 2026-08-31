load("//bazel/helm:defs.bzl", "helm_chart")

filegroup(
    name = "values_yaml",
    srcs = ["values.yaml"],
    visibility = ["//projects/embervm/runtimes/claude:__pkg__"],
)

filegroup(
    name = "gke_values",
    srcs = ["values-gke.yaml"],
    visibility = ["//projects/inference:__subpackages__"],
)

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)
