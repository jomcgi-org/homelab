load("@bazel_skylib//:bzl_library.bzl", "bzl_library")

# Prebuilt Erlang/OTP toolchain. Its only user, the EmberVM control plane,
# was deleted with the hub (HOME-262; tag hub-final keeps it).
# See repositories.bzl for why a prebuilt (not from-source) OTP is used here.

# Smoke test: proves the prebuilt ubuntu-22.04 OTP boots and runs crypto ON the
# RBE executor (the decisive de-risk before wiring mix/elixir + apko). Built by
# `bazel test //...`. It was no-remote-cache "to re-verify the live executor",
# which meant it and the other toolchain smokes re-ran on EVERY
# invocation (PR, queue candidate, main, write-back) with unchanged inputs. The inputs are hermetic, so it caches like everything else
# now; the executor was de-risked in 2026-07.
genrule(
    name = "otp_smoke",
    srcs = [
        "@otp_ubuntu2204_amd64//:otp",
        "@otp_ubuntu2204_amd64//:Install",
    ],
    outs = ["otp_smoke.txt"],
    cmd = "$(location otp_smoke.sh) \"$(location @otp_ubuntu2204_amd64//:Install)\" \"$@\"",
    # "verification" marks a suite that is not a Bazel test rule, for
    # bazel/tools/ci/affected-targets.sh (#5538). INERT on this target as things
    # stand: its only main-repo source is otp_smoke.sh, and any changed file
    # under bazel/ takes the //... fallback before the query runs, so the tag can
    # never be what selects it. Kept so the marking is uniform across the suites,
    # and so it starts working if that fallback is ever narrowed.
    tags = ["verification"],
    tools = ["otp_smoke.sh"],
)

bzl_library(
    name = "repositories",
    srcs = ["repositories.bzl"],
    visibility = ["//visibility:public"],
    deps = ["@bazel_tools//tools/build_defs/repo:http.bzl"],
)
