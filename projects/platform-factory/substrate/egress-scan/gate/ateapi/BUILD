# A minimal subset of the Agent Substrate control API (see ateapi.proto). The
# generated Go files are checked in, like projects/embervm/proto, so the
# package builds without a protoc toolchain. Gazelle must not turn ateapi.proto
# into proto rules.
# gazelle:proto disable

load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "ateapi",
    srcs = [
        "ateapi.pb.go",
        "ateapi_grpc.pb.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/gate/ateapi",
    visibility = ["//projects/platform-factory/substrate/egress-scan/gate:__pkg__"],
    deps = [
        "@org_golang_google_grpc//:grpc",
        "@org_golang_google_grpc//codes",
        "@org_golang_google_grpc//status",
        "@org_golang_google_protobuf//reflect/protoreflect",
        "@org_golang_google_protobuf//runtime/protoimpl",
        "@org_golang_google_protobuf//types/known/timestamppb",
    ],
)

exports_files(["ateapi.proto"])
