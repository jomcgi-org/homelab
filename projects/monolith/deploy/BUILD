load("//bazel/helm:defs.bzl", "helm_admissibility_render")

filegroup(
    name = "deploy_values",
    srcs = ["values.yaml"],
    visibility = ["//projects/monolith:__subpackages__"],
)

# The prod values under their own visible file label, so the dev Application's
# render can layer them first (application.yaml valueFiles order is prod
# values, then dev overrides). exports_files rather than a filegroup: a rule
# named "values.yaml" would shadow the source file of the same name in this
# package and cycle.
exports_files(
    ["values.yaml"],
    visibility = ["//projects/monolith:__subpackages__"],
)

filegroup(
    name = "application",
    srcs = ["application.yaml"],
    visibility = ["//projects/monolith:__subpackages__"],
)

# Rendered for //bazel/helm:chart_admissibility_test (#4831). Release name and
# namespace mirror deploy/application.yaml.
helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/monolith/chart",
    chart_files = "//projects/monolith/chart:chart",
    release_name = "monolith",
    namespace = "monolith",
    values_files = ["values.yaml"],
)
