load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    # The vendored upstream subchart is linted by its own repo, not ours.
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "polylane",
    chart = "projects/platform/polylane",
    chart_files = ":chart",
    namespace = "polylane",
    release_name = "polylane-k8s",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
    ],
)
