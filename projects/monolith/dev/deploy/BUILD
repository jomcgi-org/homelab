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
    namespace = "monolith-dev",
    release_name = "monolith-dev",
    values_files = [
        "//projects/monolith/deploy:deploy_values",
        ":deploy_values",
    ],
)

filegroup(
    name = "application",
    srcs = ["application.yaml"],
    visibility = ["//projects/monolith:__subpackages__"],
)

filegroup(
    name = "recovery_gke_values",
    srcs = ["values-recovery-gke.yaml"],
    visibility = ["//projects/monolith/chart:__pkg__"],
)

filegroup(
    name = "recovery_support_manifests",
    srcs = [
        "recovery-network-policy.yaml",
        "recovery-secrets.yaml",
        "values-recovery-gke.yaml",
    ],
    visibility = ["//projects/monolith:__pkg__"],
)
