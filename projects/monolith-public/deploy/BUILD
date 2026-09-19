load("//bazel/helm:defs.bzl", "helm_admissibility_render")

filegroup(
    name = "deploy_values",
    srcs = ["values.yaml"],
    visibility = [
        "//projects/monolith:__subpackages__",
        "//projects/monolith-public/chart:__pkg__",
    ],
)

helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/monolith-public/chart",
    chart_files = "//projects/monolith-public/chart:chart",
    namespace = "monolith-public",
    release_name = "monolith-public",
    values_files = [":deploy_values"],
)

helm_admissibility_render(
    name = "render_manifests_gke",
    chart = "projects/monolith-public/chart",
    chart_files = "//projects/monolith-public/chart:chart",
    namespace = "monolith-public",
    release_name = "monolith-public",
    target_suffix = "_gke",
    values_files = [
        ":deploy_values",
        ":gke_values",
    ],
)

filegroup(
    name = "gke_values",
    srcs = ["values-gke.yaml"],
    visibility = [
        "//projects/monolith:__subpackages__",
        "//projects/monolith-public/chart:__pkg__",
    ],
)
