load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "nvidia-gpu-operator",
    chart = "projects/platform/nvidia-gpu-operator",
    chart_files = ":chart",
    namespace = "gpu-operator",
    release_name = "nvidia-gpu-operator",
    tags = [
        "helm",
        "template",
    ],
    values_files = ["values.yaml"],
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
