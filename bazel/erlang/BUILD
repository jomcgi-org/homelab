load("@bazel_skylib//:bzl_library.bzl", "bzl_library")

# Prebuilt Erlang/OTP toolchain groundwork for the EmberVM control plane.
# See repositories.bzl for why a prebuilt (not from-source) OTP is used here.

# Smoke test: proves the prebuilt ubuntu-22.04 OTP boots and runs crypto ON the
# RBE executor (the decisive de-risk before wiring mix/elixir + apko). Built by
# `bazel test //...`; no-remote-cache so it re-verifies the live executor.
genrule(
    name = "otp_smoke",
    srcs = [
        "@otp_ubuntu2204_amd64//:otp",
        "@otp_ubuntu2204_amd64//:Install",
    ],
    outs = ["otp_smoke.txt"],
    cmd = "$(location otp_smoke.sh) \"$(location @otp_ubuntu2204_amd64//:Install)\" \"$@\"",
    tags = ["no-remote-cache"],
    tools = ["otp_smoke.sh"],
)

bzl_library(
    name = "repositories",
    srcs = ["repositories.bzl"],
    visibility = ["//visibility:public"],
    deps = ["@bazel_tools//tools/build_defs/repo:http.bzl"],
)
