load("//bazel/helm:defs.bzl", "helm_admissibility_render", "helm_chart")

helm_chart(
    name = "chart",
    visibility = ["//overlays:__subpackages__"],
)

# Rendered for //bazel/helm:chart_admissibility_test (#4831). Release name and
# namespace mirror application.yaml.
helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/platform/signoz-addons/alerts",
    chart_files = ":chart",
    release_name = "signoz-alerts",
    namespace = "signoz",
    values_files = ["values.yaml"],
)
