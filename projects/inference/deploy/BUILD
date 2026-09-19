load("//bazel/helm:defs.bzl", "helm_admissibility_render", "helm_chart")

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

helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/inference/deploy",
    chart_files = ":chart",
    namespace = "inference",
    release_name = "inference",
    values_files = ["values.yaml"],
)

helm_admissibility_render(
    name = "render_manifests_gke",
    chart = "projects/inference/deploy",
    chart_files = ":chart",
    namespace = "inference",
    release_name = "inference",
    target_suffix = "_gke",
    values_files = [
        "values.yaml",
        ":gke_values",
    ],
)
