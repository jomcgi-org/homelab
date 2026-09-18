load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

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

# The generated deploy/BUILD target covers the home composition. Keep the GKE
# render here, where a same-package chart reference cannot lose visibility when
# the Helm Gazelle extension refreshes the deploy package.
argocd_app(
    name = "inference-gke",
    chart = "projects/inference/chart",
    chart_files = ":chart",
    generate_manifests = False,
    generate_semgrep = False,
    namespace = "inference",
    release_name = "inference",
    target_suffix = "_gke",
    values_files = [
        "values.yaml",
        "//projects/inference/deploy:gke_values",
    ],
)
