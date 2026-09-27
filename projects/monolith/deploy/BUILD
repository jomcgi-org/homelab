load("//bazel/helm:defs.bzl", "helm_admissibility_render")

filegroup(
    name = "deploy_values",
    srcs = ["values.yaml"],
    visibility = ["//projects/monolith:__subpackages__"],
)

helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/monolith/chart",
    chart_files = "//projects/monolith/chart:chart",
    namespace = "monolith",
    release_name = "monolith",
    values_files = [":deploy_values"],
)

helm_admissibility_render(
    name = "render_manifests_gke",
    chart = "projects/monolith/chart",
    chart_files = "//projects/monolith/chart:chart",
    namespace = "monolith",
    release_name = "monolith",
    target_suffix = "_gke",
    values_files = [
        ":deploy_values",
        ":gke_values",
    ],
)

filegroup(
    name = "gke_values",
    srcs = ["values-gke.yaml"],
    visibility = ["//projects/monolith:__subpackages__"],
)

filegroup(
    name = "application",
    srcs = ["application.yaml"],
    visibility = ["//projects/monolith:__subpackages__"],
)

# Markdown read by the monolith doc-manifest genrules (#6446). See
# //projects/monolith:repo_docs_srcs.
filegroup(
    name = "repo_docs",
    srcs = glob(
        ["**/*.md"],
        allow_empty = True,
        exclude = ["**/node_modules/**"],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)
