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

# Render the same chart and values hierarchy declared by the home Application.
argocd_app(
    name = "inference",
    chart = "projects/inference/chart",
    chart_files = ":chart",
    generate_manifests = False,
    generate_semgrep = False,
    namespace = "inference",
    release_name = "inference",
    values_files = ["values.yaml"],
)

# Render the GKE composition too. The overlay disables the NInfer workload and
# keeps the embeddings workload, so both deployment shapes stay covered.
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
