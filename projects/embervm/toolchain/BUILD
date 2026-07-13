load("@rules_shell//shell:sh_test.bzl", "sh_test")

# Toolchain feasibility probe (see probe.sh). Runs as a bazel test action ON the
# RBE executor, reporting which from-source OTP-27 build prerequisites the stock
# executor provides. It underpins the Task 1 toolchain design; a green result
# means the OCaml-style from-source pattern applies directly, a red result names
# exactly what must be provisioned. Tagged no-remote-cache so it re-probes the
# live executor rather than serving a stale cached verdict.
sh_test(
    name = "otp_build_probe",
    srcs = ["probe.sh"],
    tags = ["no-remote-cache"],
)
