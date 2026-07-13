load("@bazel_skylib//:bzl_library.bzl", "bzl_library")

# Prebuilt Erlang/OTP toolchain groundwork for the EmberVM control plane.
# See repositories.bzl for why a prebuilt (not from-source) OTP is used here.

# The mix_release driver is consumed by the control package's release genrule.
exports_files(
    ["mix_release.sh"],
    visibility = ["//projects/embervm:__subpackages__"],
)

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

# Proves the prebuilt OTP + precompiled Elixir compile and ExUnit-test the
# control-plane skeleton on the executor (the other half of the Task 1 toolchain).
genrule(
    name = "mix_test_smoke",
    srcs = [
        "@otp_ubuntu2204_amd64//:otp",
        "@otp_ubuntu2204_amd64//:Install",
        "@elixir_1_18_4//:elixir",
        "@elixir_1_18_4//:bin/elixir",
        "//projects/embervm/control:srcs",
        "//projects/embervm/control:mix.exs",
    ],
    outs = ["mix_test_smoke.txt"],
    cmd = "$(location mix_test.sh) \"$(location @otp_ubuntu2204_amd64//:Install)\" \"$(location @elixir_1_18_4//:bin/elixir)\" \"$(location //projects/embervm/control:mix.exs)\" \"$@\"",
    tags = ["no-remote-cache"],
    tools = ["mix_test.sh"],
)

bzl_library(
    name = "repositories",
    srcs = ["repositories.bzl"],
    visibility = ["//visibility:public"],
    deps = ["@bazel_tools//tools/build_defs/repo:http.bzl"],
)
