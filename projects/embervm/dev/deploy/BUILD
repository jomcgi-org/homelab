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

# The dev Application, exposed to the chart tests.
filegroup(
    name = "application",
    srcs = ["application.yaml"],
    visibility = ["//projects/embervm/chart:__pkg__"],
)

# Rendered for //bazel/helm:chart_admissibility_test (#4831). The dev
# Application's valueFiles list carries only dev/deploy/values.yaml, so the
# render mirrors that exactly.
helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/embervm/chart",
    chart_files = "//projects/embervm/chart:chart",
    release_name = "embervm-dev",
    namespace = "embervm-dev",
    values_files = ["values.yaml"],
)
