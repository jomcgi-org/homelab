load("//bazel/helm:defs.bzl", "argocd_app", "helm_admissibility_render")

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

# The factory cluster's render, checked against the content rules (HOME-350).
argocd_app(
    name = "scotland-connector",
    chart = "projects/scotland-connector/chart",
    chart_files = "//projects/scotland-connector/chart:chart",
    content_rules = True,  # keep
    generate_manifests = False,
    namespace = "scotland-connector",
    release_name = "scotland-connector",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
        "//projects/scotland-connector/chart:image_values",
        "//projects/scotland-connector/factory:values.yaml",
    ],  # keep
)
