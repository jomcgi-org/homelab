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

# The sha256 of the factory chart's render of these values (ArgoCD's: no
# CRDs, the chart version label normalized). connector_test.py checks it, so
# a change to what the cluster gets is made deliberately; regenerate with
# the command its failure prints.
filegroup(
    name = "render_lock",
    srcs = ["render.sha256"],
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
