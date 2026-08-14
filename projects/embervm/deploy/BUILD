# Exposed so the ADR 023 6b cleartext-lane guard in the claude runtime package can
# read the egress credential catalog. Mirrors
# projects/firecracker/substrate/deploy:deploy_values, which the fc-invoke guard
# reads the same way. Also exposed for the chart_env_identity_test.
filegroup(
    name = "deploy_values",
    srcs = ["values.yaml"],
    visibility = [
        "//projects/embervm/chart:__pkg__",
        "//projects/embervm/runtimes/claude:__pkg__",
    ],
)

# The production Application, exposed to the chart tests.
filegroup(
    name = "application",
    srcs = ["application.yaml"],
    visibility = ["//projects/embervm/chart:__pkg__"],
)
