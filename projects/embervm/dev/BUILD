# Exposed so the ADR 023 6b cleartext-lane guard in the claude runtime package can
# read the egress credential catalog. Mirrors
# projects/firecracker/substrate/deploy:deploy_values, which the fc-invoke guard
# reads the same way.
filegroup(
    name = "deploy_values",
    srcs = ["values.yaml"],
    visibility = ["//projects/embervm/runtimes/claude:__pkg__"],
)
