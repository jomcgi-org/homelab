load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "polylane",
    chart = "projects/platform/polylane",
    chart_files = "//projects/platform/polylane:chart",
    namespace = "polylane",
    release_name = "polylane-k8s",
    tags = [
        "helm",
        "template",
    ],
    values_files = ["//projects/platform/polylane:values.yaml"],
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
