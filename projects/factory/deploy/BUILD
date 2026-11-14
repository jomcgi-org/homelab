load("//bazel/helm:defs.bzl", "helm_admissibility_render")

exports_files(
    ["application.yaml"],
    visibility = ["//projects/factory/chart:__pkg__"],
)

filegroup(
    name = "deploy_values",
    srcs = ["values.yaml"],
    visibility = ["//projects/factory/chart:__pkg__"],
)

helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/factory/chart",
    chart_files = "//projects/factory/chart:chart",
    namespace = "kagent",
    release_name = "factory",
    values_files = [":deploy_values"],
)
