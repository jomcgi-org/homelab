load("//bazel/helm:defs.bzl", "argocd_app")

filegroup(
    name = "deploy_values",
    srcs = ["values.yaml"],
    visibility = ["//projects/monolith:__subpackages__"],
)

filegroup(
    name = "gke_values",
    srcs = ["values-gke.yaml"],
    visibility = ["//projects/monolith:__subpackages__"],
)

argocd_app(
    name = "monolith-public-gke",
    chart = "projects/monolith-public/chart",
    chart_files = "//projects/monolith-public/chart:chart",
    generate_manifests = False,
    generate_semgrep = False,
    namespace = "monolith-public",
    release_name = "monolith-public",
    tags = [
        "helm",
        "template",
    ],
    target_suffix = "_gke",
    values_files = [
        "values.yaml",
        "values-gke.yaml",
    ],
)
