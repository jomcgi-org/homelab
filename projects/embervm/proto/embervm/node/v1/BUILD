# node.proto is the EmberVM control-to-node gRPC contract (the design seam).
#
# Codegen is build-time only: protoc + language plugins regenerate the Go and
# Elixir stubs on every build (see :node_go_src and :node_elixir_src). Nothing
# generated is checked in, so the genrules are the single source of truth and
# offline-RBE reproducibility is by construction: there is no stale-stub drift
# to guard, and the cross-language round-trip test (//projects/embervm/control)
# proves the stubs are wire-correct.
#
# This package is hand-managed (generated srcs, a prebuilt protoc, and a
# nested-module go plugin), so gazelle is told to leave it alone entirely.
# gazelle:ignore

load("@rules_go//go:def.bzl", "go_library")

filegroup(
    name = "node_proto",
    srcs = ["node.proto"],
    # //bazel/erlang is granted access so mix_test_smoke (the general test lane)
    # can stage node.proto into the mix sandbox for the layer-1 spec vocabulary
    # test, which parses the rpc verbs out of it (ADR embervm/006 layer 1).
    visibility = [
        "//bazel/erlang:__pkg__",
        "//projects/embervm:__subpackages__",
    ],
)

# Go codegen: protoc-gen-go emits the message types (node.pb.go) and
# protoc-gen-go-grpc the service (node_grpc.pb.go). paths=source_relative writes
# both under the proto's in-package path (embervm/node/v1/), which we lift into
# the two declared outs. The plugins are Bazel-built go_binary tools from the
# module graph (protoc-gen-go-grpc is its own nested module, pinned in go.mod);
# protoc is the prebuilt @protoc_linux_x86_64 (amd64 RBE executor).
genrule(
    name = "node_go_src",
    srcs = [":node_proto"],
    outs = [
        "node.pb.go",
        "node_grpc.pb.go",
    ],
    cmd = """
set -euo pipefail
proto=$(execpath :node_proto)
root=$${proto%/embervm/node/v1/node.proto}
out=$$(mktemp -d)
$(execpath @protoc_linux_x86_64//:protoc) \
    --plugin=protoc-gen-go=$(execpath @org_golang_google_protobuf//cmd/protoc-gen-go) \
    --plugin=protoc-gen-go-grpc=$(execpath @org_golang_google_grpc_cmd_protoc_gen_go_grpc//:protoc-gen-go-grpc) \
    --go_out=$$out --go_opt=paths=source_relative \
    --go-grpc_out=$$out --go-grpc_opt=paths=source_relative \
    -I $$root $$proto
cp $$out/embervm/node/v1/node.pb.go $(execpath node.pb.go)
cp $$out/embervm/node/v1/node_grpc.pb.go $(execpath node_grpc.pb.go)
""",
    tools = [
        "@org_golang_google_grpc_cmd_protoc_gen_go_grpc//:protoc-gen-go-grpc",
        "@org_golang_google_protobuf//cmd/protoc-gen-go",
        "@protoc_linux_x86_64//:protoc",
    ],
)

go_library(
    name = "nodev1",
    srcs = [
        "node.pb.go",
        "node_grpc.pb.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1",
    visibility = ["//projects/embervm:__subpackages__"],
    deps = [
        "@org_golang_google_grpc//:grpc",
        "@org_golang_google_grpc//codes",
        "@org_golang_google_grpc//status",
        "@org_golang_google_protobuf//reflect/protoreflect",
        "@org_golang_google_protobuf//runtime/protoimpl",
    ],
)

# Elixir codegen: build the protoc-gen-elixir escript from the protobuf hex
# package (its own mix.exs defines the escript; the gRPC Service/Stub generator
# lives in protobuf itself since 0.17), then run protoc with plugins=grpc. The
# driver stages the prebuilt OTP + Elixir to compile the escript and to run it
# (protoc execs the escript, which needs `escript` from OTP on PATH). Output is
# the single node.pb.ex consumed by the round-trip test.
genrule(
    name = "node_elixir_src",
    srcs = [
        "@otp_ubuntu2204_amd64//:otp",
        "@otp_ubuntu2204_amd64//:Install",
        "@elixir_1_18_4//:elixir",
        "@elixir_1_18_4//:bin/elixir",
        "@protoc_linux_x86_64//:protoc",
        "@hex_archive//file",
        "@hex_protobuf//file",
        "//bazel/erlang:protoc_gen_elixir_mix.exs",
        ":node_proto",
    ],
    outs = ["node.pb.ex"],
    cmd = "$(location //bazel/erlang:gen_elixir.sh) " +
          "\"$(location @otp_ubuntu2204_amd64//:Install)\" " +
          "\"$(location @elixir_1_18_4//:bin/elixir)\" " +
          "\"$(location @protoc_linux_x86_64//:protoc)\" " +
          "\"$(location //bazel/erlang:protoc_gen_elixir_mix.exs)\" " +
          "\"$(location @hex_archive//file)\" " +
          "\"$(location @hex_protobuf//file)\" " +
          "\"$(location :node_proto)\" " +
          "\"$@\"",
    tools = ["//bazel/erlang:gen_elixir.sh"],
    # //bazel/erlang also consumes this: mix_test_smoke (the general test lane)
    # stages node.pb.ex into control/lib/ so prod code referencing the stubs
    # compiles there, alongside the control:release_tar consumer under
    # //projects/embervm. Both need to see this generated source.
    visibility = [
        "//bazel/erlang:__pkg__",
        "//projects/embervm:__subpackages__",
    ],
)
