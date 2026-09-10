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

filegroup(
    name = "gke_values",
    srcs = ["values-gke.yaml"],
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

# The hub overlay, separate from deploy_values because that one is consumed
# through single-file $(location) expansions. Read by the cleartext lane guard
# to tie egress pool members to the broker's registered grants.
filegroup(
    name = "deploy_values_gke",
    srcs = ["values-gke.yaml"],
    visibility = ["//projects/embervm/runtimes/claude:__pkg__"],
)
