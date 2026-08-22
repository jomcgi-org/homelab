load("@bazel_skylib//:bzl_library.bzl", "bzl_library")

# Prebuilt Erlang/OTP toolchain groundwork for the EmberVM control plane.
# See repositories.bzl for why a prebuilt (not from-source) OTP is used here.

# The mix_release + release_boot drivers are consumed by the control package's
# release + boot-smoke genrules (kept there, alongside :release_tar).
exports_files(
    [
        "mix_release.sh",
        "release_boot.sh",
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
        "@hex_decimal//file",
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
        "@hex_postgrex//file",
        "@hex_protobuf//file",
        "@hex_telemetry//file",
        "@hex_thousand_island//file",
        "@hex_websock//file",
        # OpenTelemetry closure (Task 13).
        "@hex_acceptor_pool//file",
        "@hex_ctx//file",
        "@hex_gproc//file",
        "@hex_grpcbox//file",
        "@hex_hpack_erl//file",
        "@hex_opentelemetry//file",
        "@hex_opentelemetry_api//file",
        "@hex_opentelemetry_exporter//file",
        "@hex_opentelemetry_semantic_conventions//file",
        "@hex_ssl_verify_fun//file",
        "@hex_tls_certificate_check//file",
        "@hex_ts_chatterbox//file",
    ],
    visibility = ["//projects/embervm:__subpackages__"],
)

# Smoke test: proves the prebuilt ubuntu-22.04 OTP boots and runs crypto ON the
# RBE executor (the decisive de-risk before wiring mix/elixir + apko). Built by
# `bazel test //...`. It was no-remote-cache "to re-verify the live executor",
# which meant it, mix_test_smoke, release_boot_smoke and the claude late-resume
# smoke re-ran on EVERY invocation (PR, queue candidate, main, write-back) with
# unchanged inputs. The inputs are hermetic, so it caches like everything else
# now; the executor was de-risked in 2026-07.
genrule(
    name = "otp_smoke",
    srcs = [
        "@otp_ubuntu2204_amd64//:otp",
        "@otp_ubuntu2204_amd64//:Install",
    ],
    outs = ["otp_smoke.txt"],
    cmd = "$(location otp_smoke.sh) \"$(location @otp_ubuntu2204_amd64//:Install)\" \"$@\"",
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
        # Generated node.proto Elixir stub, staged into control/lib/ by mix_test.sh
        # so prod lib/ code referencing Embervm.Node.V1.* (Task 9's NodeRegistry)
        # compiles in the general test lane. Nothing generated is checked in.
        "//projects/embervm/proto/embervm/node/v1:node_elixir_src",
        ":hex_tarballs",
        "@rebar3//file",
        # Layer-1 spec vocabulary test inputs (ADR embervm/006): the spec sources
        # (adoption.tla + vocabulary.exs) and node.proto. mix_test.sh stages these
        # into the sandbox and exports EMBERVM_SPECS_DIR / EMBERVM_NODE_PROTO so
        # spec_vocabulary_test.exs finds them; absent env leaves behavior unchanged.
        "//projects/embervm/specs:spec_files",
        "//projects/embervm/proto/embervm/node/v1:node_proto",
        # The stateful fake noded (#4761). The restart-wedge scenario drives a
        # real Dispatcher against it and suppresses primed_vm_ids to starve
        # adoption. Without it staged here the scenario SKIPS, which would make
        # the harness's own proof inert in the lane that runs it.
        "//projects/embervm/proto/embervm/node/v1/fakenode",
    ],
    outs = ["mix_test_smoke.txt"],
    cmd = "MIX_REBAR3_SRC=\"$$(pwd)/$(location @rebar3//file)\" " +
          "EMBERVM_SPECS_SRCS=\"$(locations //projects/embervm/specs:spec_files)\" " +
          "EMBERVM_NODE_PROTO_SRC=\"$(location //projects/embervm/proto/embervm/node/v1:node_proto)\" " +
          "EMBERVM_FAKE_NODE_SRC=\"$(location //projects/embervm/proto/embervm/node/v1/fakenode)\" " +
          "$(location mix_test.sh) \"$(location @otp_ubuntu2204_amd64//:Install)\" \"$(location @elixir_1_18_4//:bin/elixir)\" \"$(location //projects/embervm/control:mix.exs)\" \"$@\" \"$(location @hex_archive//file)\" \"$(location //projects/embervm/proto/embervm/node/v1:node_elixir_src)\" $(locations :hex_tarballs)",
    tools = ["mix_test.sh"],
)

bzl_library(
    name = "repositories",
    srcs = ["repositories.bzl"],
    visibility = ["//visibility:public"],
    deps = ["@bazel_tools//tools/build_defs/repo:http.bzl"],
)
