load("//bazel/helm:defs.bzl", "helm_admissibility_render")

filegroup(
    name = "deploy_values",
    srcs = ["values.yaml"],
    visibility = ["//projects/monolith:__subpackages__"],
)

filegroup(
    name = "application",
    srcs = ["application.yaml"],
    visibility = ["//projects/monolith:__subpackages__"],
)

# Rendered for //bazel/helm:chart_admissibility_test (#4831). The dev
# Application's valueFiles order is load-bearing: production's values first,
# dev's overrides second (see application.yaml).
helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/monolith/chart",
    chart_files = "//projects/monolith/chart:chart",
    release_name = "monolith-dev",
    namespace = "monolith-dev",
    values_files = [
        "//projects/monolith/deploy:values.yaml",
        "values.yaml",
    ],
)
