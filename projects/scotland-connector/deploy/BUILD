load("//bazel/helm:defs.bzl", "helm_admissibility_render")

filegroup(
    name = "deploy_values",
    srcs = ["values.yaml"],
    visibility = ["//projects/scotland-connector/chart:__pkg__"],
)

helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/scotland-connector/chart",
    chart_files = "//projects/scotland-connector/chart:chart",
    namespace = "scotland-connector",
    release_name = "scotland-connector",
    values_files = [":deploy_values"],
)
