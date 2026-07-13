load("@bazel_skylib//:bzl_library.bzl", "bzl_library")

# Prebuilt Erlang/OTP toolchain groundwork for the EmberVM control plane.
# See repositories.bzl for why a prebuilt (not from-source) OTP is used here.

# The mix_release driver is consumed by the control package's release genrule.
exports_files(
    [
        "mix_release.sh",
        # node.proto Elixir codegen: the driver builds the protoc-gen-elixir
        # escript from the protobuf hex package and runs protoc (see
        # //projects/embervm/proto/embervm/node/v1:node_elixir_src).
        "gen_elixir.sh",
        "protoc_gen_elixir_mix.exs",
        # Cross-language node.proto round-trip test driver (see
        # //projects/embervm/proto/embervm/node/v1/roundtrip:node_roundtrip_test).
        "roundtrip_test.sh",
    ],
    visibility = ["//projects/embervm:__subpackages__"],
)

# The control-plane hex dependency closure (see repositories.bzl). Fetched as raw
# hex tarballs; the mix_test.sh / mix_release.sh drivers unpack them into deps/ and
# mix consumes them as path deps. Passed as trailing genrule args to both drivers.
filegroup(
    name = "hex_tarballs",
    srcs = [
        "@hex_bandit//file",
        "@hex_cc_precompiler//file",
        "@hex_db_connection//file",
        "@hex_elixir_make//file",
        "@hex_exqlite//file",
        "@hex_finch//file",
        "@hex_googleapis//file",
        "@hex_grpc//file",
        "@hex_grpc_core//file",
        "@hex_hpax//file",
        "@hex_jason//file",
        "@hex_mime//file",
        "@hex_mint//file",
        "@hex_nimble_options//file",
        "@hex_nimble_pool//file",
        "@hex_plug//file",
        "@hex_plug_crypto//file",
        "@hex_protobuf//file",
        "@hex_telemetry//file",
        "@hex_thousand_island//file",
        "@hex_websock//file",
    ],
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
        "@hex_archive//file",
        ":hex_tarballs",
    ],
    outs = ["mix_test_smoke.txt"],
    cmd = "$(location mix_test.sh) \"$(location @otp_ubuntu2204_amd64//:Install)\" \"$(location @elixir_1_18_4//:bin/elixir)\" \"$(location //projects/embervm/control:mix.exs)\" \"$@\" \"$(location @hex_archive//file)\" $(locations :hex_tarballs)",
    tags = ["no-remote-cache"],
    tools = ["mix_test.sh"],
)

bzl_library(
    name = "repositories",
    srcs = ["repositories.bzl"],
    visibility = ["//visibility:public"],
    deps = ["@bazel_tools//tools/build_defs/repo:http.bzl"],
)
