filegroup(
    name = "deploy_values",
    srcs = ["values.yaml"],
    visibility = ["//projects/monolith:__subpackages__"],
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
