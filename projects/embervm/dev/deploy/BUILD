load("//bazel/helm:defs.bzl", "helm_admissibility_render")

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

helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/embervm/chart",
    chart_files = "//projects/embervm/chart:chart",
    namespace = "embervm-dev",
    release_name = "embervm-dev",
    values_files = [":deploy_values"],
)

# The GKE hub overlay for embervm-dev, loaded after deploy_values by
# projects/gke-apps/embervm-dev/application.yaml. Exposed to the chart tests.
filegroup(
    name = "gke_values",
    srcs = ["values-gke.yaml"],
    visibility = ["//projects/embervm/chart:__pkg__"],
)

# The dev Application, exposed to the chart tests.
filegroup(
    name = "application",
    srcs = ["application.yaml"],
    visibility = ["//projects/embervm/chart:__pkg__"],
)

# The INACTIVE GKE recovery preset, exposed to chart and consumer-join tests.
# No Application or kustomization references it; no live deployment loads it.
filegroup(
    name = "recovery_values",
    srcs = ["values-recovery-gke.yaml"],
    visibility = [
        "//projects/embervm/chart:__pkg__",
        "//projects/monolith:__pkg__",
    ],
)

# INACTIVE GKE store-validation preset for #6193. No Application or
# kustomization references it. It is exposed only to the chart regression test
# until an operator completes the live checklist and reviews an activation.
filegroup(
    name = "store_validation_values",
    srcs = ["values-store-validation-gke.yaml"],
    visibility = ["//projects/embervm/chart:__pkg__"],
)

exports_files(
    ["kustomization.yaml"],
    visibility = ["//projects/embervm/chart:__pkg__"],
)
