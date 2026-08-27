load("//bazel/helm:defs.bzl", "helm_admissibility_render", "helm_chart")

filegroup(
    name = "values_yaml",
    srcs = ["values.yaml"],
    visibility = ["//projects/embervm/runtimes/claude:__pkg__"],
)

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

# Rendered for //bazel/helm:chart_admissibility_test (#4831). Release name and
# namespace mirror deploy/application.yaml; chart and overlay are colocated.
helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/inference/deploy",
    chart_files = ":chart",
    release_name = "inference",
    namespace = "inference",
    values_files = ["values.yaml"],
)
