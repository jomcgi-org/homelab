# Exposed so the ADR 023 6b cleartext-lane guard in the claude runtime package can
# read the egress credential catalog. Also exposed for the chart_env_identity_test.
filegroup(
    name = "deploy_values",
    srcs = ["values.yaml"],
    visibility = [
        "//projects/embervm/chart:__pkg__",
        "//projects/embervm/runtimes/claude:__pkg__",
    ],
)

# The dev Application, exposed to the chart tests.
filegroup(
    name = "application",
    srcs = ["application.yaml"],
    visibility = ["//projects/embervm/chart:__pkg__"],
)

# The INACTIVE GKE recovery preset, exposed only to the chart render test.
# No Application or kustomization references it; no live deployment loads it.
filegroup(
    name = "recovery_values",
    srcs = ["values-recovery-gke.yaml"],
    visibility = ["//projects/embervm/chart:__pkg__"],
)
