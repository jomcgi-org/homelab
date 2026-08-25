load("//bazel/helm:defs.bzl", "helm_admissibility_render")

# Exposed so the ADR 023 6b cleartext-lane guard in the claude runtime package can
# read the egress credential catalog. Mirrors
# projects/firecracker/substrate/deploy:deploy_values, which the fc-invoke guard
# reads the same way. Also exposed for the chart_env_identity_test, and for the
# shotter package's baked-allowlist-vs-chart-allowlist subset guard (issue #4994
# review round).
filegroup(
    name = "deploy_values",
    srcs = ["values.yaml"],
    visibility = [
        "//projects/embervm/chart:__pkg__",
        "//projects/embervm/runtimes/claude:__pkg__",
        "//projects/embervm/runtimes/shotter:__pkg__",
    ],
)

# The production Application, exposed to the chart tests.
filegroup(
    name = "application",
    srcs = ["application.yaml"],
    visibility = ["//projects/embervm/chart:__pkg__"],
)

# Rendered for //bazel/helm:chart_admissibility_test (#4831). Release name and
# namespace mirror deploy/application.yaml.
helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/embervm/chart",
    chart_files = "//projects/embervm/chart:chart",
    release_name = "embervm",
    namespace = "embervm",
    values_files = ["values.yaml"],
)
