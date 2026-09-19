load("//bazel/helm:defs.bzl", "helm_admissibility_render")

filegroup(
    name = "deploy_values",
    srcs = ["values.yaml"],
    visibility = ["//projects/monolith:__subpackages__"],
)

helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/monolith/chart",
    chart_files = "//projects/monolith/chart:chart",
    namespace = "monolith",
    release_name = "monolith",
    values_files = [":deploy_values"],
)

helm_admissibility_render(
    name = "render_manifests_gke",
    chart = "projects/monolith/chart",
    chart_files = "//projects/monolith/chart:chart",
    namespace = "monolith",
    release_name = "monolith",
    target_suffix = "_gke",
    values_files = [
        ":deploy_values",
        ":gke_values",
    ],
)

filegroup(
    name = "gke_values",
    srcs = ["values-gke.yaml"],
    visibility = ["//projects/monolith:__subpackages__"],
)

filegroup(
    name = "application",
    srcs = ["application.yaml"],
    visibility = ["//projects/monolith:__subpackages__"],
)
