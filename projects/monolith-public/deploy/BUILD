load("//bazel/helm:defs.bzl", "helm_admissibility_render")

filegroup(
    name = "deploy_values",
    srcs = ["values.yaml"],
    visibility = ["//projects/monolith:__subpackages__"],
)

# Rendered for //bazel/helm:chart_admissibility_test (#4831). Release name and
# namespace mirror deploy/application.yaml.
helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/monolith-public/chart",
    chart_files = "//projects/monolith-public/chart:chart",
    release_name = "monolith-public",
    namespace = "monolith-public",
    values_files = ["values.yaml"],
)
